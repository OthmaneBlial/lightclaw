from pathlib import Path

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
    original_read_text = Path.read_text

    def read_text(path, *args, **kwargs):
        if path == unreadable:
            raise PermissionError("fixture unreadable file")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)

    result = load_personality(str(workspace))
    assert result == "Legacy identity"
    assert "private content" not in result


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
