from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from core.artifacts import initialize_artifact_repository
from core.workspaces import (
    WorkspaceSafetyError,
    _remove_owned_tree_at,
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
    monkeypatch.setattr("core.artifacts.subprocess.run", run)

    assert capture_git_checkpoint(tmp_path)["is_git"] is False

    child_env = run.call_args.kwargs["env"]
    assert "TELEGRAM_BOT_TOKEN" not in child_env
    assert "LIGHTCLAW_TEST_SECRET" not in child_env
    assert child_env["LIGHTCLAW_DELEGATED"] == "1"


def test_task_git_checkpoint_does_not_use_parent_repository_or_monitor(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "source.txt").write_text("source repository")
    initialize_artifact_repository(source, "parent-checkpoint")
    monitor = tmp_path / "monitor"
    marker = tmp_path / "outside-monitor-marker"
    monitor.write_text(
        "#!/bin/sh\nprintf ran > "
        + shlex.quote(str(marker))
        + "\nprintf 'token\\000/\\000'\n"
    )
    monitor.chmod(0o700)
    subprocess.run(["git", "-C", str(source), "config", "core.fsmonitor", str(monitor)], check=True)
    subprocess.run(["git", "-C", str(source), "config", "core.fsmonitorHookVersion", "2"], check=True)
    subprocess.run(["git", "-C", str(source), "status", "--porcelain"], check=True, capture_output=True)
    assert marker.exists(), "fixture must exercise the configured parent monitor"
    marker.unlink()
    parent_head = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    parent_index = (source / ".git" / "index").read_bytes()
    task = source / "20261001_120000_new-task"
    task.mkdir()

    checkpoint = capture_git_checkpoint(task)

    assert checkpoint == {"is_git": False, "commit": None, "dirty": False, "status": []}
    assert not marker.exists()
    assert (source / ".git" / "index").read_bytes() == parent_index
    assert subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip() == parent_head


@pytest.mark.parametrize("parent_alias", [False, True])
def test_task_registration_cannot_claim_a_symlink_target_for_undo(tmp_path: Path, parent_alias):
    actual = tmp_path / "actual"
    actual.mkdir()
    parent = tmp_path / "alias" if parent_alias else actual
    if parent_alias:
        parent.symlink_to(actual, target_is_directory=True)
    root = parent / "workspace"
    root.mkdir()
    user_project = root / "user-project"
    user_project.mkdir()
    user_file = user_project / "keep.txt"
    user_file.write_text("pre-existing user data")
    candidate = root / "20261001_120000_new-task"
    candidate.symlink_to(user_project, target_is_directory=True)

    with pytest.raises(WorkspaceSafetyError, match="real directory"):
        register_task_workspace(root, candidate, "fresh task")

    assert not (root / ".lightclaw-meta" / f"{user_project.name}.json").exists()
    assert not (root / ".lightclaw-meta" / f"{candidate.name}.json").exists()
    with pytest.raises(WorkspaceSafetyError, match="no LightClaw ownership record"):
        undo_owned_task(root, user_project.name, apply=True)
    assert user_file.read_text() == "pre-existing user data"
    assert candidate.is_symlink()


@pytest.mark.parametrize("parent_alias", [False, True])
def test_owned_task_undo_is_dry_run_by_default_and_scoped(tmp_path: Path, parent_alias):
    root = tmp_path / "workspace"
    if parent_alias:
        actual = tmp_path / "actual"
        actual.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(actual, target_is_directory=True)
        root = alias / "workspace"
    root.mkdir()
    owned = root / "20260823_120000_safe-task"
    owned.mkdir()
    (owned / "created.txt").write_text("agent output", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    keep = outside / "keep.txt"
    keep.write_text("user data", encoding="utf-8")
    (owned / "escape").symlink_to(outside, target_is_directory=True)
    sibling = root / "user-project"
    sibling.mkdir()
    user_file = sibling / "keep.txt"
    user_file.write_text("pre-existing", encoding="utf-8")
    register_task_workspace(root, owned, "safe task")
    metadata_dir = root / ".lightclaw-meta"
    assert metadata_dir.stat().st_mode & 0o777 == 0o700
    assert (metadata_dir / f"{owned.name}.json").stat().st_mode & 0o777 == 0o600

    preview = undo_owned_task(root, owned.name)
    assert preview["applied"] is False
    assert (owned / "created.txt").is_file()

    applied = undo_owned_task(root, owned.name, apply=True)
    assert applied["applied"] is True
    assert not owned.exists()
    assert keep.read_text(encoding="utf-8") == "user data"
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


def test_undo_does_not_follow_root_symlink_swap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "workspace"
    root.mkdir()
    owned = root / "20260930_120000_safe-task"
    owned.mkdir()
    register_task_workspace(root, owned, "safe task")

    outside = tmp_path / "outside"
    outside.mkdir()
    outside_task = outside / owned.name
    outside_task.mkdir()
    keep = outside_task / "keep.txt"
    keep.write_text("user data", encoding="utf-8")
    moved_root = tmp_path / "workspace-original"
    remove_tree = _remove_owned_tree_at

    def swap_root_then_remove(root_fd, name):
        root.rename(moved_root)
        root.symlink_to(outside, target_is_directory=True)
        return remove_tree(root_fd, name)

    monkeypatch.setattr("core.workspaces._remove_owned_tree_at", swap_root_then_remove)
    undo_owned_task(root, owned.name, apply=True)

    assert keep.read_text(encoding="utf-8") == "user data"
    metadata = json.loads(
        (moved_root / ".lightclaw-meta" / f"{owned.name}.json").read_text(encoding="utf-8")
    )
    assert metadata["state"] == "undone"
    assert not (outside / ".lightclaw-meta").exists()


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


def test_private_workspace_dir_does_not_open_swapped_symlink_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "workspace"
    root.mkdir()
    private = root / "private"
    victim = tmp_path / "victim"
    victim.mkdir()
    victim.chmod(0o755)
    open_file = os.open
    swapped = False

    def swap_then_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == "private" and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            private.rmdir()
            private.symlink_to(victim, target_is_directory=True)
        return open_file(path, flags, *args, **kwargs)

    monkeypatch.setattr("core.fs.os.open", swap_then_open)
    with pytest.raises(WorkspaceSafetyError, match="must not be a symlink"):
        ensure_private_workspace_dir(root, "private")

    assert swapped
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
