from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.bot.delegation.execution import DelegationExecutionMixin
from core.jobs import JobStore
from core.workspaces import WorkspaceSafetyError


class ExecutionHarness(DelegationExecutionMixin):
    def __init__(self):
        self.config = SimpleNamespace(
            local_agent_capability_profile="workspace-write",
            local_agent_progress_interval_sec=30,
        )


class TimeoutHarness(ExecutionHarness):
    def __init__(self, *, resistant_child: bool = False):
        super().__init__()
        self.config.local_agent_timeout_sec = 1
        self.resistant_child = resistant_child

    @staticmethod
    def _build_delegation_prompt(task: str, workspace: Path | None = None) -> str:
        return task

    def _build_local_agent_command(
        self,
        agent: str,
        workspace: Path,
        prompt: str,
        stream_output: bool,
        capability_profile: str | None = None,
    ):
        child_code = (
            "import pathlib,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "pathlib.Path('child-ready.txt').write_text('ready'); time.sleep(2); "
            "pathlib.Path('child-survived.txt').write_text('bad')"
            if self.resistant_child
            else (
                "import pathlib,time; time.sleep(2); "
                "pathlib.Path('child-survived.txt').write_text('bad')"
            )
        )
        parent_code = (
            "import subprocess,sys,time; "
            f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
            "time.sleep(30)"
        )
        return [sys.executable, "-c", parent_code], None


def test_codex_capability_profiles_map_to_sandbox_flags(tmp_path: Path):
    harness = ExecutionHarness()

    observe, _ = harness._build_local_agent_command(
        "codex", tmp_path, "task", False, "observe"
    )
    workspace, _ = harness._build_local_agent_command(
        "codex", tmp_path, "task", False, "workspace-write"
    )
    trusted, _ = harness._build_local_agent_command(
        "codex", tmp_path, "task", False, "trusted-command"
    )

    assert observe[observe.index("--sandbox") + 1] == "read-only"
    assert workspace[workspace.index("--sandbox") + 1] == "workspace-write"
    assert "--dangerously-bypass-approvals-and-sandbox" not in observe
    assert "--dangerously-bypass-approvals-and-sandbox" not in workspace
    assert "--dangerously-bypass-approvals-and-sandbox" in trusted


def test_claude_capability_profiles_map_to_permission_modes(tmp_path: Path):
    harness = ExecutionHarness()

    observe, _ = harness._build_local_agent_command(
        "claude", tmp_path, "task", False, "observe"
    )
    workspace, _ = harness._build_local_agent_command(
        "claude", tmp_path, "task", False, "workspace-write"
    )
    trusted, _ = harness._build_local_agent_command(
        "claude", tmp_path, "task", False, "trusted-command"
    )

    assert observe[observe.index("--permission-mode") + 1] == "plan"
    assert workspace[workspace.index("--permission-mode") + 1] == "acceptEdits"
    assert "--dangerously-skip-permissions" not in observe
    assert "--dangerously-skip-permissions" not in workspace
    assert "--dangerously-skip-permissions" in trusted


def test_invalid_profile_falls_back_to_workspace_sandbox(tmp_path: Path):
    harness = ExecutionHarness()
    command, _ = harness._build_local_agent_command(
        "codex", tmp_path, "task", False, "not-real"
    )
    assert command[command.index("--sandbox") + 1] == "workspace-write"


def test_receipt_output_refuses_symlinked_private_metadata_subdirectory(tmp_path: Path):
    root = tmp_path / "workspace"
    (root / ".lightclaw-meta").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".lightclaw-meta" / "receipts").symlink_to(outside, target_is_directory=True)
    harness = ExecutionHarness()
    harness.config.workspace_path = str(root)

    with pytest.raises(WorkspaceSafetyError, match="must not be a symlink"):
        harness._receipt_output_dir("run-123")

    assert list(outside.iterdir()) == []


async def test_streaming_timeout_kills_worker_process_group_and_preserves_existing_files(
    tmp_path: Path,
):
    harness = TimeoutHarness()
    existing = tmp_path / "existing.txt"
    existing.write_text("user data", encoding="utf-8")

    result = await harness._invoke_local_agent_streaming(
        "codex", "task", workspace=tmp_path
    )
    time.sleep(2.3)

    assert result["timed_out"] is True
    assert result["exit_code"] == 124
    assert not (tmp_path / "child-survived.txt").exists()
    assert existing.read_text(encoding="utf-8") == "user data"


async def test_streaming_timeout_also_covers_prompt_stdin_write(tmp_path: Path):
    harness = TimeoutHarness()
    harness._build_delegation_prompt = lambda task, workspace=None: task
    harness._build_local_agent_command = lambda **_kwargs: (
        [sys.executable, "-c", "import time; time.sleep(30)"],
        "x" * (1024 * 1024),
    )

    result = await asyncio.wait_for(
        harness._invoke_local_agent_streaming("codex", "large prompt", workspace=tmp_path),
        timeout=2,
    )

    assert result["timed_out"] is True
    assert result["exit_code"] == 124


async def test_task_cancellation_kills_term_resistant_worker_process_group(tmp_path: Path):
    harness = TimeoutHarness(resistant_child=True)
    harness.config.local_agent_timeout_sec = 30
    task = asyncio.create_task(
        harness._invoke_local_agent_streaming("codex", "task", workspace=tmp_path)
    )
    for _ in range(100):
        if (tmp_path / "child-ready.txt").exists():
            break
        await asyncio.sleep(0.02)
    assert (tmp_path / "child-ready.txt").exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(2.3)
    assert not (tmp_path / "child-survived.txt").exists()


async def test_cancellation_during_process_registration_kills_and_unregisters_worker(
    tmp_path: Path,
):
    class BlockingProcessStore:
        def __init__(self):
            self.registration_started = threading.Event()
            self.allow_registration = threading.Event()
            self.registered = False
            self.unregistered: list[tuple[str, int]] = []

        def register_process_group(self, _run_id: str, _pid: int) -> None:
            self.registration_started.set()
            if not self.allow_registration.wait(timeout=5):
                raise TimeoutError("registration fixture timed out")
            self.registered = True

        def unregister_process_group(self, run_id: str, pid: int) -> None:
            self.registered = False
            self.unregistered.append((run_id, pid))

    harness = TimeoutHarness()
    harness.config.local_agent_timeout_sec = 30
    harness.jobs = BlockingProcessStore()
    harness._build_local_agent_command = lambda **_kwargs: (
        [
            sys.executable,
            "-c",
            "import pathlib,time; pathlib.Path('registration-ready.txt').touch(); "
            "time.sleep(.6); pathlib.Path('registration-survived.txt').touch()",
        ],
        None,
    )
    task = asyncio.create_task(
        harness._invoke_local_agent_streaming(
            "codex", "task", workspace=tmp_path, job_run_id="run-race"
        )
    )
    try:
        for _ in range(100):
            if harness.jobs.registration_started.is_set() and (
                tmp_path / "registration-ready.txt"
            ).exists():
                break
            await asyncio.sleep(.02)
        assert harness.jobs.registration_started.is_set()
        assert (tmp_path / "registration-ready.txt").exists()

        task.cancel()
        await asyncio.sleep(.05)
        harness.jobs.allow_registration.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(.7)

        assert not (tmp_path / "registration-survived.txt").exists()
        assert harness.jobs.registered is False
        assert len(harness.jobs.unregistered) == 1
        assert harness.jobs.unregistered[0][0] == "run-race"
    finally:
        harness.jobs.allow_registration.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_streaming_output_is_bounded_and_reports_truncation(tmp_path: Path):
    harness = TimeoutHarness()
    harness.config.local_agent_timeout_sec = 5
    final_event = json.dumps(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "completed"}}
    ).encode() + b"\n"
    script = (
        "import os; line=b'x'*4096+b'\\n'; "
        "[os.write(1, line) for _ in range(768)]; "
        "os.write(1, b'y'*1200000+b'\\n'); "
        f"os.write(1, {final_event!r})"
    )
    harness._build_delegation_prompt = lambda task, workspace=None: task
    harness._build_local_agent_command = lambda **_kwargs: (
        [sys.executable, "-c", script],
        None,
    )

    result = await harness._invoke_local_agent_streaming("codex", "task", workspace=tmp_path)

    assert result.get("output_truncated") is True
    assert len(result["stdout"].encode("utf-8")) <= 2 * 1024 * 1024
    assert "completed" in result["summary"]
    assert "truncated" in result["summary"].lower()


def test_delegation_unregisters_process_group_after_completion(tmp_path: Path):
    harness = TimeoutHarness()
    harness.config.local_agent_timeout_sec = 5
    harness._build_local_agent_command = lambda **_kwargs: (
        [sys.executable, "-c", "import time; time.sleep(.1); print('ok')"],
        None,
    )
    harness.jobs = JobStore(tmp_path / "jobs.db")
    job = harness.jobs.create_job(
        workspace=tmp_path / "repo",
        session_id="fixture",
        goal="fixture",
        approved_scope="fixture",
        risk_level="low",
        capability_profile="workspace-write",
        plan=[{"label": "worker", "depends_on": [], "owned_paths": [], "idempotent": False, "resumable": False}],
        status="queued",
    )
    harness.jobs.claim_next(workspace=tmp_path / "repo", worker_pid=os.getpid())
    try:
        result = asyncio.run(
            harness._invoke_local_agent_streaming(
                "codex", "fixture", workspace=tmp_path, job_run_id=job["run_id"]
            )
        )
        assert result["ok"]
        assert harness.jobs.db.execute(
            "SELECT 1 FROM job_process_groups WHERE run_id = ?", (job["run_id"],)
        ).fetchone() is None
    finally:
        harness.jobs.close()
