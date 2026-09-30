from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.bot.delegation.workspace import DelegationWorkspaceMixin
from core.workspaces import WorkspaceSafetyError


class WorkspaceHarness(DelegationWorkspaceMixin):
    def __init__(self, workspace_path: Path):
        self.config = SimpleNamespace(workspace_path=str(workspace_path))


def test_task_workspace_retries_atomic_name_collision(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    expected = root / "20260930_120000_same-task"
    original_mkdir = Path.mkdir
    collided = False

    def simulate_concurrent_creator(path, *args, **kwargs):
        nonlocal collided
        if path == expected and not collided:
            collided = True
            original_mkdir(path, *args, **kwargs)
            raise FileExistsError(path)
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr("core.bot.delegation.workspace.time.strftime", lambda *_: "20260930_120000")
    monkeypatch.setattr(Path, "mkdir", simulate_concurrent_creator)

    created = WorkspaceHarness(root)._create_task_workspace("same task")

    assert created == root / "20260930_120000_same-task_2"
    assert expected.is_dir()
    assert created.is_dir()
    assert (root / ".lightclaw-meta" / f"{created.name}.json").is_file()


def test_task_workspace_removes_empty_dir_when_registration_fails(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "metadata-outside"
    outside.mkdir()
    (root / ".lightclaw-meta").symlink_to(outside, target_is_directory=True)
    expected = root / "20260930_120000_same-task"
    monkeypatch.setattr("core.bot.delegation.workspace.time.strftime", lambda *_: "20260930_120000")

    with pytest.raises(WorkspaceSafetyError, match="must not be a symlink"):
        WorkspaceHarness(root)._create_task_workspace("same task")

    assert not expected.exists()
    assert list(outside.iterdir()) == []
