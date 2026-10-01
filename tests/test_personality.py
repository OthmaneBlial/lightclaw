from pathlib import Path

from core import personality
from core.constants import FALLBACK_IDENTITY
from core.personality import load_personality, resolve_runtime_path


def test_runtime_files_precede_legacy_workspace_and_keep_fixed_order(tmp_path):
    runtime_root = tmp_path / "runtime"
    workspace = runtime_root / "workspace"
    runtime_root.mkdir()
    workspace.mkdir()
    for filename, value in (
        ("IDENTITY.md", "Runtime identity"),
        ("SOUL.md", "Runtime soul"),
        ("USER.md", "Runtime user"),
    ):
        (runtime_root / filename).write_text(value, encoding="utf-8")
        (workspace / filename).write_text(f"Legacy {value}", encoding="utf-8")

    assert load_personality(str(workspace)) == (
        "Runtime identity\n\n---\n\nRuntime soul\n\n---\n\nRuntime user"
    )


def test_empty_runtime_file_falls_back_to_legacy_content(tmp_path):
    runtime_root = tmp_path / "runtime"
    workspace = runtime_root / "workspace"
    runtime_root.mkdir()
    workspace.mkdir()
    (runtime_root / "IDENTITY.md").write_text(" \n", encoding="utf-8")
    (workspace / "IDENTITY.md").write_text("Legacy identity", encoding="utf-8")

    assert load_personality(str(workspace)) == "Legacy identity"


def test_unreadable_runtime_file_falls_back_without_exposing_contents(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    workspace = runtime_root / "workspace"
    runtime_root.mkdir()
    workspace.mkdir()
    unreadable = runtime_root / "IDENTITY.md"
    unreadable.write_text("private content must not escape", encoding="utf-8")
    (workspace / "IDENTITY.md").write_text("Legacy identity", encoding="utf-8")
    read_bounded_at = personality.read_text_bounded_at

    def read_bounded(root, relative, *_args, **_kwargs):
        if Path(root) / relative == unreadable:
            raise PermissionError("fixture unreadable file")
        return read_bounded_at(root, relative, *_args, **_kwargs)

    monkeypatch.setattr("core.personality.read_text_bounded_at", read_bounded)

    result = load_personality(str(workspace))
    assert result == "Legacy identity"
    assert "private content" not in result


def test_personality_rejects_symlinked_profile_and_falls_back_safely(tmp_path):
    runtime_root = tmp_path / "runtime"
    workspace = runtime_root / "workspace"
    runtime_root.mkdir()
    workspace.mkdir()
    outside_secret = tmp_path / "credentials.json"
    outside_secret.write_text('{"api_key":"must-not-enter-the-prompt"}', encoding="utf-8")
    (runtime_root / "IDENTITY.md").symlink_to(outside_secret)
    (workspace / "IDENTITY.md").write_text("Safe legacy identity", encoding="utf-8")

    result = load_personality(str(workspace))

    assert result == "Safe legacy identity"
    assert "must-not-enter-the-prompt" not in result


def test_oversized_runtime_personality_falls_back_to_legacy(tmp_path, monkeypatch, caplog):
    runtime_root = tmp_path / "runtime"
    workspace = runtime_root / "workspace"
    runtime_root.mkdir()
    workspace.mkdir()
    (runtime_root / "IDENTITY.md").write_bytes(b"x" * (16 * 1024 + 1))
    (workspace / "IDENTITY.md").write_text("Legacy identity", encoding="utf-8")
    monkeypatch.setattr("core.personality.MAX_PERSONALITY_FILE_BYTES", 16 * 1024)

    assert load_personality(str(workspace)) == "Legacy identity"
    assert "Ignoring IDENTITY.md because it exceeds" in caplog.text


def test_relative_runtime_path_uses_isolated_lightclaw_home(tmp_path, monkeypatch):
    home = tmp_path / "isolated-home"
    monkeypatch.setenv("LIGHTCLAW_HOME", str(home))

    assert resolve_runtime_path("workspace/memory.db") == home / "workspace" / "memory.db"


def test_all_empty_personality_files_use_safe_fallback(tmp_path):
    runtime_root = tmp_path / "runtime"
    workspace = runtime_root / "workspace"
    runtime_root.mkdir()
    workspace.mkdir()
    for filename in ("IDENTITY.md", "SOUL.md", "USER.md"):
        (runtime_root / filename).write_text("", encoding="utf-8")

    assert load_personality(str(workspace)) == FALLBACK_IDENTITY
