from __future__ import annotations

from pathlib import Path

import pytest

from core.workspaces import (
    WorkspaceSafetyError,
    register_task_workspace,
    resolve_owned_task,
    undo_owned_task,
    validate_workspace_root,
)


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


def test_workspace_root_refuses_filesystem_root_and_symlink(tmp_path: Path):
    with pytest.raises(WorkspaceSafetyError, match="filesystem root"):
        validate_workspace_root(Path(Path.cwd().anchor))

    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    with pytest.raises(WorkspaceSafetyError, match="symlink"):
        validate_workspace_root(linked)
