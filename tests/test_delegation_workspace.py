from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.bot import LightClawBot
from core.bot.delegation.workspace import DelegationWorkspaceMixin
from core.jobs import JobStore
from core.workspaces import WorkspaceSafetyError


class WorkspaceHarness(DelegationWorkspaceMixin):
    def __init__(self, workspace_path: Path):
        self.config = SimpleNamespace(workspace_path=str(workspace_path))


@pytest.mark.asyncio
async def test_new_delegation_does_not_claim_another_sessions_queued_job(tmp_path):
    workspace = tmp_path / "repo"
    store = JobStore(tmp_path / "jobs.db")
    older = store.create_job(
        workspace=workspace,
        session_id="other-chat",
        goal="older reviewed task",
        approved_scope="fixture",
        risk_level="low",
        capability_profile="workspace-write",
        plan=[],
        status="queued",
    )
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(
        workspace_path=str(tmp_path),
        local_agent_progress_interval_sec=10,
        local_agent_capability_profile="workspace-write",
    )
    bot.jobs = store
    bot._available_local_agents = lambda: {"codex": "/fixture/codex"}
    bot._delegation_safety_block_reason = lambda _task: ""

    async def checkpoint(*_args, **_kwargs):
        return {}

    async def refuse_launch(*_args, **_kwargs):
        raise AssertionError("queued delegation must not launch an agent")

    bot._prepare_task_workspace_checkpoint = checkpoint
    bot._invoke_local_agent_streaming = refuse_launch
    try:
        result = await bot._run_local_agent_task_impl(
            "new-chat", "codex", "new reviewed task", workspace_dir=workspace
        )
        assert "queued" in result
        assert "will not auto-start" in result
        assert store.get_job(older["run_id"])["status"] == "queued"
        jobs = store.list_jobs(workspace=workspace)
        assert len(jobs) == 2
        assert all(job["status"] == "queued" for job in jobs)
        assert bot._active_run_ids_by_session == {}
    finally:
        store.close()


@pytest.mark.parametrize("swap", ["leaf", "parent"])
def test_receipt_file_hash_rejects_symlink_swap(tmp_path, monkeypatch, swap):
    from core.fs import sha256_file

    workspace = tmp_path / "workspace"
    folder = workspace / "result"
    folder.mkdir(parents=True)
    target = folder / "output.txt"
    target.write_text("approved workspace output")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / target.name).write_text("private outside output")

    def swap_before_hash(*args, **kwargs):
        if swap == "parent":
            folder.rename(workspace / "original")
            folder.symlink_to(outside, target_is_directory=True)
        else:
            target.unlink()
            target.symlink_to(outside / target.name)
        return sha256_file(*args, **kwargs)

    monkeypatch.setattr("core.bot.delegation.workspace.sha256_file", swap_before_hash)
    changes = WorkspaceHarness._workspace_file_changes(
        workspace, {}, {"result/output.txt": (25, 1)}
    )
    assert changes[0]["sha256"] == "unavailable"


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


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 3])
async def test_cancelled_single_agent_workspace_creation_removes_late_directory(
    tmp_path, monkeypatch, cancel_count
):
    root = tmp_path / "workspace"
    root.mkdir()
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(
        workspace_path=str(root),
        local_agent_progress_interval_sec=10,
        local_agent_capability_profile="workspace-write",
    )
    bot._available_local_agents = lambda: {"codex": "/fixture/codex"}
    bot._delegation_safety_block_reason = lambda _task: ""
    started = threading.Event()
    release = threading.Event()
    from core.bot.delegation.workspace import register_task_workspace

    def delayed_register(workspace_root, candidate, goal):
        started.set()
        assert release.wait(timeout=5)
        register_task_workspace(workspace_root, candidate, goal)

    monkeypatch.setattr(
        "core.bot.delegation.workspace.register_task_workspace", delayed_register
    )
    execution = asyncio.create_task(
        bot._run_local_agent_task_impl("456", "codex", "cancel during single setup")
    )
    try:
        assert await asyncio.to_thread(started.wait, 5)
        for _ in range(cancel_count):
            execution.cancel()
            await asyncio.sleep(0)
            assert not execution.done()
        release.set()

        with pytest.raises(asyncio.CancelledError):
            await execution
    finally:
        release.set()
        await asyncio.gather(execution, return_exceptions=True)

    assert [path.name for path in root.iterdir()] == [".lightclaw-meta"]
    metadata = list((root / ".lightclaw-meta").glob("*.json"))
    assert len(metadata) == 1
    assert json.loads(metadata[0].read_text(encoding="utf-8"))["state"] == "undone"
