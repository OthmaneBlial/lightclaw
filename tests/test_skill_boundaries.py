from __future__ import annotations

import io
import json
import zipfile

import pytest

from lightclaw_cli import build_parser
from skills import (
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
