from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from core.workspaces import (
    WorkspaceSafetyError,
    capture_git_checkpoint,
    ensure_private_workspace_dir,
    register_task_workspace,
    resolve_owned_task,
    undo_owned_task,
    validate_workspace_root,
)


def test_git_checkpoint_uses_secret_free_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-secret")
    monkeypatch.setenv("LIGHTCLAW_TEST_SECRET", "provider-secret")
    run = Mock(return_value=subprocess.CompletedProcess([], 1, "", ""))
    monkeypatch.setattr("core.workspaces.subprocess.run", run)

    assert capture_git_checkpoint(tmp_path)["is_git"] is False

    child_env = run.call_args.kwargs["env"]
    assert "TELEGRAM_BOT_TOKEN" not in child_env
    assert "LIGHTCLAW_TEST_SECRET" not in child_env
    assert child_env["LIGHTCLAW_DELEGATED"] == "1"


def test_owned_task_undo_is_dry_run_by_default_and_scoped(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    owned = root / "20260823_120000_safe-task"
    owned.mkdir()
    (owned / "created.txt").write_text("agent output", encoding="utf-8")
    sibling = root / "user-project"
    sibling.mkdir()
    user_file = sibling / "keep.txt"
    user_file.write_text("pre-existing", encoding="utf-8")
    register_task_workspace(root, owned, "safe task")

    preview = undo_owned_task(root, owned.name)
    assert preview["applied"] is False
    assert (owned / "created.txt").is_file()

    applied = undo_owned_task(root, owned.name, apply=True)
    assert applied["applied"] is True
    assert not owned.exists()
    assert user_file.read_text(encoding="utf-8") == "pre-existing"


def test_undo_refuses_unregistered_symlink_and_traversal(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("safe", encoding="utf-8")

    with pytest.raises(WorkspaceSafetyError, match="single safe"):
        resolve_owned_task(root, "../outside")

    link = root / "fake-task"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceSafetyError, match="ownership record"):
        undo_owned_task(root, link.name, apply=True)
    assert (outside / "keep.txt").is_file()


def test_task_metadata_directory_symlink_cannot_read_or_write_outside_root(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    owned = root / "20260823_120000_safe-task"
    owned.mkdir()
    register_task_workspace(root, owned, "safe task")

    outside = tmp_path / "metadata-outside"
    (root / ".lightclaw-meta").rename(outside)
    (root / ".lightclaw-meta").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceSafetyError, match="must not be a symlink"):
        resolve_owned_task(root, owned.name)

    another = root / "20260823_120001_another-task"
    another.mkdir()
    with pytest.raises(WorkspaceSafetyError, match="must not be a symlink"):
        register_task_workspace(root, another, "another task")
    assert not (outside / f"{another.name}.json").exists()


def test_private_workspace_dir_does_not_chmod_swapped_symlink_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "workspace"
    root.mkdir()
    private = root / "private"
    victim = tmp_path / "victim"
    victim.mkdir()
    victim.chmod(0o755)
    chmod = os.chmod

    def swap_then_chmod(path, mode, *, dir_fd=None, follow_symlinks=True):
        target = Path(path)
        if target == private:
            target.rmdir()
            target.symlink_to(victim, target_is_directory=True)
        return chmod(path, mode, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr("core.workspaces.os.chmod", swap_then_chmod)
    with pytest.raises(WorkspaceSafetyError, match="must not be a symlink"):
        ensure_private_workspace_dir(root, "private")

    assert victim.stat().st_mode & 0o777 == 0o755


def test_undo_rejects_oversized_task_ownership_record(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    owned = root / "20260823_120000_safe-task"
    owned.mkdir()
    register_task_workspace(root, owned, "safe task")
    metadata_path = root / ".lightclaw-meta" / f"{owned.name}.json"
    metadata_path.write_bytes(b" " * (1024 * 1024 + 1))

    with pytest.raises(WorkspaceSafetyError, match="ownership record is unreadable"):
        undo_owned_task(root, owned.name, apply=True)
    assert owned.is_dir()


def test_workspace_root_refuses_filesystem_root_and_symlink(tmp_path: Path):
    with pytest.raises(WorkspaceSafetyError, match="filesystem root"):
        validate_workspace_root(Path(Path.cwd().anchor))

    missing_target = tmp_path / "missing-target"
    broken_link = tmp_path / "broken-link"
    broken_link.symlink_to(missing_target, target_is_directory=True)
    with pytest.raises(WorkspaceSafetyError, match="must not be a symlink"):
        validate_workspace_root(broken_link)
    assert not missing_target.exists()

    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    with pytest.raises(WorkspaceSafetyError, match="symlink"):
        validate_workspace_root(linked)
