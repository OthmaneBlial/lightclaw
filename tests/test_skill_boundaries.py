from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import skills as skills_module
from core.bot import LightClawBot
from lightclaw_cli import build_parser
from skills import (
    MAX_DOWNLOAD_BYTES,
    MAX_SKILL_TEXT_BYTES,
    SkillError,
    SkillManager,
    validate_skill_directory,
    validate_skill_manifest,
)


def _bundle(
    skill_text: bytes,
    member: str = "nested/SKILL.md",
    manifest: dict[str, object] | None = None,
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, skill_text)
        archive.writestr("nested/_meta.json", '{"version":"1.0.0"}')
        if manifest is not None:
            archive.writestr("nested/skill.json", json.dumps(manifest))
    return buffer.getvalue()


def test_skill_bundle_reads_only_bounded_manifest_content():
    text, metadata, manifest = SkillManager._extract_zip_bundle(
        _bundle(b"---\nname: safe\n---\n\n# Safe skill\n")
    )

    assert "# Safe skill" in text
    assert metadata == {"version": "1.0.0"}
    assert manifest is None


def test_skill_bundle_rejects_zip_bomb_sized_manifest():
    oversized = b"a" * (MAX_SKILL_TEXT_BYTES + 1)

    with pytest.raises(SkillError, match="uncompressed size limit"):
        SkillManager._extract_zip_bundle(_bundle(oversized))


def _manager(tmp_path):
    return SkillManager(
        workspace_path=str(tmp_path / "runtime" / "workspace"),
        skills_state_path=str(tmp_path / "runtime" / "skills_state.json"),
    )


def _safe_manifest(skill_id: str = "safe") -> dict[str, object]:
    return {
        "schema_version": 1,
        "id": skill_id,
        "name": "Safe skill",
        "version": "1.2.3",
        "owner": "fixture-owner",
        "capabilities": ["prompt-guidance"],
        "network": {"allowed": False, "domains": []},
        "writable_paths": [],
        "dependencies": [],
    }


def test_local_skill_requires_hash_review_and_stale_approval_is_removed(tmp_path):
    manager = _manager(tmp_path)
    record = manager.create_local_skill("Review Helper", "Review evidence")
    preview = manager.preview_activation(record.skill_id)

    assert preview["valid"] is True
    assert preview["capabilities"] == ["prompt-guidance"]
    assert preview["network"] == {"allowed": False, "domains": []}
    assert preview["version"] == "0.1.0"
    assert len(str(preview["content_sha256"])) == 64
    with pytest.raises(SkillError, match="content-hash token"):
        manager.activate("chat", record.skill_id, "wrong")

    manager.activate("chat", record.skill_id, str(preview["activation_token"]))
    assert manager.list_active("chat") == [record.skill_id]
    assert "Permission boundary: prompt-guidance only" in manager.prompt_context("chat")

    record.skill_path.write_text("# Changed after approval\n", encoding="utf-8")
    assert manager.prompt_context("chat") == ""
    assert manager.list_active("chat") == []


@pytest.mark.parametrize("replacement", ["modified", "symlink"])
def test_active_skill_rechecks_reviewed_bytes_before_prompt_use(
    tmp_path, monkeypatch, replacement
):
    manager = _manager(tmp_path)
    record = manager.create_local_skill("Review Helper", "Review evidence")
    preview = manager.preview_activation(record.skill_id)
    manager.activate("chat", record.skill_id, str(preview["activation_token"]))
    active_records = manager.active_records
    outside = tmp_path / "credentials.txt"
    outside.write_text("external credential must not enter the prompt", encoding="utf-8")

    def validate_then_replace(chat_id):
        active = active_records(chat_id)
        record.skill_path.unlink()
        if replacement == "symlink":
            record.skill_path.symlink_to(outside)
        else:
            record.skill_path.write_text("unreviewed instructions", encoding="utf-8")
        return active

    monkeypatch.setattr(manager, "active_records", validate_then_replace)
    context = manager.prompt_context("chat")

    assert context == ""
    assert "credential" not in context
    assert "unreviewed instructions" not in context


def test_skill_preview_rejects_symlink_swap_after_hash_validation(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    record = manager.create_local_skill("Review Helper", "Review evidence")
    secret = tmp_path / "credentials.txt"
    secret.write_text("credential preview must be refused", encoding="utf-8")
    validate = skills_module.validate_skill_directory
    manager.resolve_skill = lambda _ref: record

    def validate_then_swap(path):
        report = validate(path)
        record.skill_path.unlink()
        record.skill_path.symlink_to(secret)
        return report

    monkeypatch.setattr(skills_module, "validate_skill_directory", validate_then_swap)
    preview = manager.preview_activation(record.skill_id)

    assert preview["valid"] is False
    assert preview["activation_token"] is None
    assert preview["source_preview"] == ""
    assert "credential" not in preview["source_preview"]


@pytest.mark.parametrize("unsafe_file", ["oversized", "symlink-swap"])
def test_legacy_skill_migration_skips_unsafe_instruction_file(tmp_path, monkeypatch, unsafe_file):
    runtime = tmp_path / "runtime"
    workspace = runtime / "workspace"
    directory = runtime / "skills" / "local" / "legacy-skill"
    directory.mkdir(parents=True)
    skill_path = directory / "SKILL.md"
    skill_path.write_text("# Legacy skill\n", encoding="utf-8")

    if unsafe_file == "oversized":
        skill_path.write_bytes(b"x" * (MAX_SKILL_TEXT_BYTES + 1))
    else:
        secret = tmp_path / "outside.txt"
        secret.write_text("private content", encoding="utf-8")
        read_skill_file = skills_module._read_skill_file

        def swap_before_read(path, name, max_bytes):
            if Path(path) == directory and name == "SKILL.md":
                skill_path.unlink()
                skill_path.symlink_to(secret)
            return read_skill_file(path, name, max_bytes)

        monkeypatch.setattr(skills_module, "_read_skill_file", swap_before_read)

    SkillManager(
        workspace_path=str(workspace),
        skills_state_path=str(runtime / "skills_state.json"),
    )

    assert not (directory / "skill.json").exists()


@pytest.mark.parametrize("unsafe_file", ["symlink", "symlink-swap", "oversized"])
def test_legacy_skill_copy_skips_symlinked_or_oversized_files(
    tmp_path, monkeypatch, unsafe_file
):
    runtime = tmp_path / "runtime"
    workspace = runtime / "workspace"
    directory = workspace / "skills" / "local" / "legacy-skill"
    directory.mkdir(parents=True)
    skill_path = directory / "SKILL.md"
    if unsafe_file in {"symlink", "symlink-swap"}:
        secret = tmp_path / "outside.txt"
        secret.write_text("private content", encoding="utf-8")
        if unsafe_file == "symlink":
            skill_path.symlink_to(secret)
        else:
            skill_path.write_text("# Legacy skill\n", encoding="utf-8")
            open_regular_file = skills_module.open_regular_file_at

            def swap_before_open(root, relative):
                if (
                    Path(root) == workspace / "skills"
                    and Path(relative).as_posix() == "local/legacy-skill/SKILL.md"
                ):
                    skill_path.unlink()
                    skill_path.symlink_to(secret)
                return open_regular_file(root, relative)

            monkeypatch.setattr(skills_module, "open_regular_file_at", swap_before_open)
    else:
        skill_path.write_bytes(b"x" * (MAX_DOWNLOAD_BYTES + 1))

    SkillManager(
        workspace_path=str(workspace),
        skills_state_path=str(runtime / "skills_state.json"),
    )

    copied = runtime / "skills" / "local" / "legacy-skill" / "SKILL.md"
    assert not copied.exists()


def test_legacy_skill_copy_preserves_regular_files(tmp_path):
    runtime = tmp_path / "runtime"
    workspace = runtime / "workspace"
    directory = workspace / "skills" / "local" / "legacy-skill"
    directory.mkdir(parents=True)
    skill = directory / "SKILL.md"
    asset = directory / "example.bin"
    skill.write_text("# Legacy skill\n", encoding="utf-8")
    asset.write_bytes(b"legacy asset")

    SkillManager(
        workspace_path=str(workspace),
        skills_state_path=str(runtime / "skills_state.json"),
    )

    copied = runtime / "skills" / "local" / "legacy-skill"
    assert (copied / "SKILL.md").read_bytes() == skill.read_bytes()
    assert (copied / "example.bin").read_bytes() == asset.read_bytes()
    assert (copied / "skill.json").is_file()


@pytest.mark.parametrize(
    "broken_state",
    [
        "{broken",
        '{"active_by_chat":[]}',
        '{"active_by_chat":{"chat":[1]}}',
        '{"approved_hashes_by_chat":{"chat":[]}}',
    ],
)
def test_corrupt_skill_state_is_preserved_and_activation_fails_closed(
    tmp_path, broken_state
):
    manager = _manager(tmp_path)
    record = manager.create_local_skill("Review Helper", "Review evidence")
    preview = manager.preview_activation(record.skill_id)
    state_path = tmp_path / "runtime" / "skills_state.json"
    state_path.write_text(broken_state, encoding="utf-8")

    with pytest.raises(SkillError, match="skills state"):
        manager.activate("chat", record.skill_id, str(preview["activation_token"]))

    with pytest.raises(SkillError, match="skills state"):
        manager.remove_skill(record.skill_id)

    assert state_path.read_text(encoding="utf-8") == broken_state
    assert record.directory.is_dir()


def test_remove_active_skill_deactivates_it_before_deleting_files(tmp_path):
    manager = _manager(tmp_path)
    record = manager.create_local_skill("Review Helper", "Review evidence")
    preview = manager.preview_activation(record.skill_id)
    manager.activate("chat", record.skill_id, str(preview["activation_token"]))

    removed = manager.remove_skill(record.skill_id)

    assert removed.skill_id == record.skill_id
    assert not record.directory.exists()
    assert manager.list_active("chat") == []


@pytest.mark.asyncio
async def test_invalid_skill_state_stops_chat_before_llm_request():
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(memory_top_k=5)
    bot._session_id_from_update = lambda _update: "chat"
    bot._log_user_message = Mock()
    bot._log_bot_message = Mock()
    bot._heartbeat_last_chat_id = ""
    bot._agent_mode_by_session = {}
    bot._get_pending_multi_plan = lambda _session_id: None
    bot._llm_backoff_active = lambda: False
    bot.memory = SimpleNamespace(
        recall=Mock(return_value=[]),
        format_memories_for_prompt=Mock(return_value=""),
        get_recent=Mock(return_value=[]),
    )
    bot._filter_recalled_memories = lambda memories: memories
    bot._clean_orphan_messages = lambda messages: messages
    bot._filter_recent_context = lambda messages: messages
    bot._get_session_summary = AsyncMock(return_value="")

    def invalid_state(_session_id):
        raise SkillError("skills state is invalid; no changes were made")

    bot.skills = SimpleNamespace(prompt_context=invalid_state)
    bot.llm = SimpleNamespace(chat=AsyncMock())
    bot._send_response = AsyncMock()
    update = SimpleNamespace(
        effective_chat=None,
        message=SimpleNamespace(reply_text=AsyncMock(return_value=None)),
    )

    await bot._process_user_message(update, SimpleNamespace(), "hello")

    bot.llm.chat.assert_not_awaited()
    bot._send_response.assert_awaited_once()
    assert "not sent to an agent" in bot._send_response.await_args.args[2]


def test_manifest_change_invalidates_existing_approval(tmp_path):
    manager = _manager(tmp_path)
    record = manager.create_local_skill("Manifest Review", "Review permissions")
    preview = manager.preview_activation(record.skill_id)
    manager.activate("chat", record.skill_id, str(preview["activation_token"]))

    manifest_path = record.directory / "skill.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["owner"] = "changed-owner"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    changed = manager.preview_activation(record.skill_id)
    assert changed["activation_token"] != preview["activation_token"]
    assert manager.active_records("chat") == []
    assert manager.list_active("chat") == []


def test_high_authority_skill_validates_but_remains_isolated(tmp_path):
    manager = _manager(tmp_path)
    directory = manager.local_dir / "networked"
    directory.mkdir()
    (directory / "SKILL.md").write_text("# Networked\n", encoding="utf-8")
    manifest = _safe_manifest("networked")
    manifest.update(
        {
            "capabilities": ["prompt-guidance", "network"],
            "network": {"allowed": True, "domains": ["api.example.com"]},
        }
    )
    (directory / "skill.json").write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "source.json").write_text(
        json.dumps({"source": "local", "version": "1.2.3", "owner": "fixture-owner"}),
        encoding="utf-8",
    )

    preview = manager.preview_activation("local/networked")
    assert preview["valid"] is True
    assert preview["isolated_only"] is True
    with pytest.raises(SkillError, match="isolated external runner"):
        manager.activate(
            "chat",
            "local/networked",
            str(preview["activation_token"]),
        )


def test_manifest_rejects_traversal_unpinned_dependencies_and_implicit_network():
    manifest = _safe_manifest()
    manifest.update(
        {
            "capabilities": ["prompt-guidance"],
            "network": {"allowed": True, "domains": []},
            "writable_paths": ["../outside"],
            "dependencies": ["requests"],
        }
    )
    errors = validate_skill_manifest(manifest)

    assert any("network access requires" in error for error in errors)
    assert any("invalid writable path" in error for error in errors)
    assert any("pin an exact version" in error for error in errors)
    assert any("subprocess capability" in error for error in errors)


def test_hub_install_pins_provenance_and_stages_atomically(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    manifest = _safe_manifest("release-check")
    payload = _bundle(b"# Release check\n", manifest=manifest)
    monkeypatch.setattr(
        manager,
        "_http_get_json",
        lambda _url: {
            "skill": {"displayName": "Release check", "summary": "fixture"},
            "latestVersion": {"version": "2.4.1"},
            "owner": {"handle": "owner-name", "userId": "owner-id"},
        },
    )
    monkeypatch.setattr(manager, "_http_get_bytes", lambda *_args, **_kwargs: payload)

    record, replaced = manager.install_from_hub("release-check")
    source = json.loads((record.directory / "source.json").read_text(encoding="utf-8"))
    installed_manifest = json.loads(
        (record.directory / "skill.json").read_text(encoding="utf-8")
    )

    assert replaced is False
    assert record.version == "2.4.1"
    assert installed_manifest["version"] == "2.4.1"
    assert installed_manifest["owner"] == "owner-name"
    assert source["provenance"]["version"] == "2.4.1"
    assert len(source["download_sha256"]) == 64
    assert source["content_sha256"] == record.content_sha256
    assert not any(path.name.startswith(".release-check") for path in manager.hub_dir.iterdir())


def test_validator_refuses_symlinked_skill_and_cli_exposes_contract(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("# Outside\n", encoding="utf-8")
    (outside / "skill.json").write_text(json.dumps(_safe_manifest()), encoding="utf-8")
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)

    report = validate_skill_directory(linked)
    parsed = build_parser().parse_args(["skills", "validate", "--path", "examples/safe-skill"])

    assert report["valid"] is False
    assert "symlinked" in report["errors"][0]
    assert parsed.skills_action == "validate"
    assert parsed.path == "examples/safe-skill"


def test_skill_validator_rejects_recursively_nested_manifest(tmp_path, monkeypatch):
    directory = tmp_path / "nested-manifest"
    directory.mkdir()
    (directory / "SKILL.md").write_text("# Safe skill\n", encoding="utf-8")
    (directory / "skill.json").write_text("{}", encoding="utf-8")

    def reject_nested_json(_content):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr("skills.json.loads", reject_nested_json)
    report = validate_skill_directory(directory)

    assert report["valid"] is False
    assert "JSON nesting limit" in report["errors"][0]


@pytest.mark.asyncio
async def test_skill_list_splits_large_html_reply():
    bot = LightClawBot.__new__(LightClawBot)
    bot.is_update_allowed = Mock(return_value=True)
    bot._privileged_rate_limited = Mock(return_value=False)
    bot._session_id_from_update = Mock(return_value="456")
    bot._log_user_message = Mock()
    skill_ids = [f"skill-{index:03}" for index in range(60)]
    bot._render_skills_overview = Mock(
        return_value="<b>Skills</b>\n"
        + "\n".join(
            f"• <code>{skill_id}</code> — " + "Useful workflow guidance. " * 8
            for skill_id in skill_ids
        )
    )
    bot._reply_logged = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=456, type="private"),
        message=SimpleNamespace(),
    )

    await bot.cmd_skills(update, SimpleNamespace(args=[]))

    messages = [call.args[1] for call in bot._reply_logged.await_args_list]
    assert len(messages) > 1
    assert all(len(message.encode("utf-16-le")) // 2 < 4096 for message in messages)
    assert all(skill_id in "".join(messages) for skill_id in skill_ids)
