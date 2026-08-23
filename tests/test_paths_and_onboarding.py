from __future__ import annotations

import stat
from types import SimpleNamespace

from core.paths import config_path, legacy_config_path, runtime_dir
from core.workspaces import register_task_workspace
from lightclaw_cli import (
    _managed_uninstall_targets,
    _write_private_text,
    cmd_onboard,
    cmd_undo,
    cmd_uninstall,
)


def test_cli_undo_defaults_to_preview_and_requires_owned_task(tmp_path, monkeypatch):
    root = tmp_path / ".lightclaw" / "workspace"
    owned = root / "20260823_120000_cli-task"
    owned.mkdir(parents=True)
    (owned / "result.txt").write_text("keep until apply", encoding="utf-8")
    register_task_workspace(root, owned, "cli task")
    monkeypatch.delenv("WORKSPACE_PATH", raising=False)

    preview = cmd_undo(
        SimpleNamespace(home=str(tmp_path), task=owned.name, apply=False, dry_run=True)
    )
    assert preview == 0
    assert owned.is_dir()

    applied = cmd_undo(
        SimpleNamespace(home=str(tmp_path), task=owned.name, apply=True, dry_run=False)
    )
    assert applied == 0
    assert not owned.exists()


def test_app_specific_paths_with_explicit_home(tmp_path):
    assert config_path(tmp_path) == tmp_path / ".config" / "lightclaw" / "config.env"
    assert legacy_config_path(tmp_path) == tmp_path / ".env"
    assert runtime_dir(tmp_path) == tmp_path / ".lightclaw"


def test_private_writer_is_atomic_private_and_backed_up(tmp_path):
    target = tmp_path / "config" / "config.env"
    _write_private_text(target, "TOKEN=first\n")
    backup = _write_private_text(target, "TOKEN=second\n", backup=True)

    assert target.read_text(encoding="utf-8") == "TOKEN=second\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert backup is not None
    assert backup.read_text(encoding="utf-8") == "TOKEN=first\n"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_onboard_uses_app_config_and_keeps_legacy_source(tmp_path, monkeypatch):
    legacy = tmp_path / ".env"
    legacy.write_text("LLM_PROVIDER=openai\nTELEGRAM_ALLOWED_USERS=123\n", encoding="utf-8")
    monkeypatch.setenv("LIGHTCLAW_DANGER_ACK", "yes")

    code = cmd_onboard(
        SimpleNamespace(
            home=str(tmp_path),
            force=False,
            reset_env=False,
            configure=False,
        )
    )

    target = tmp_path / ".config" / "lightclaw" / "config.env"
    assert code == 0
    assert target.is_file()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert legacy.is_file()
    assert legacy.read_text(encoding="utf-8").startswith("LLM_PROVIDER=openai")
    assert (tmp_path / ".lightclaw" / "workspace").is_dir()


def test_uninstall_dry_run_and_apply_preserve_user_data(tmp_path, monkeypatch):
    install_root = tmp_path / ".local" / "share" / "lightclaw"
    command = tmp_path / ".local" / "bin" / "lightclaw"
    executable = install_root / "venv" / "bin" / "lightclaw"
    executable.parent.mkdir(parents=True)
    executable.write_text("managed", encoding="utf-8")
    (install_root / ".lightclaw-install").write_text("managed-by=lightclaw\n", encoding="utf-8")
    command.parent.mkdir(parents=True)
    command.symlink_to(executable)
    app_config = tmp_path / ".config" / "lightclaw" / "config.env"
    app_config.parent.mkdir(parents=True)
    app_config.write_text("private", encoding="utf-8")
    runtime = tmp_path / ".lightclaw"
    runtime.mkdir()
    (runtime / "memory.db").write_text("private", encoding="utf-8")
    monkeypatch.setenv("LIGHTCLAW_INSTALL_ROOT", str(install_root))

    targets = _managed_uninstall_targets(tmp_path)
    assert {description for _, description in targets} == {
        "managed command symlink",
        "managed isolated environment and source",
    }

    dry_code = cmd_uninstall(
        SimpleNamespace(
            home=str(tmp_path),
            purge_data=False,
            apply=False,
            yes=False,
        )
    )
    assert dry_code == 0
    assert command.is_symlink()
    assert install_root.is_dir()

    apply_code = cmd_uninstall(
        SimpleNamespace(
            home=str(tmp_path),
            purge_data=False,
            apply=True,
            yes=False,
        )
    )
    assert apply_code == 0
    assert not command.exists()
    assert not install_root.exists()
    assert app_config.read_text(encoding="utf-8") == "private"
    assert (runtime / "memory.db").read_text(encoding="utf-8") == "private"
