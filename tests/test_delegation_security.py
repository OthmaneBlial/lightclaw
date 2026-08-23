from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.bot.delegation.execution import DelegationExecutionMixin


class ExecutionHarness(DelegationExecutionMixin):
    def __init__(self):
        self.config = SimpleNamespace(
            local_agent_capability_profile="workspace-write",
            local_agent_progress_interval_sec=30,
        )


class TimeoutHarness(ExecutionHarness):
    def __init__(self):
        super().__init__()
        self.config.local_agent_timeout_sec = 1

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
            "import pathlib,time; time.sleep(2); "
            "pathlib.Path('child-survived.txt').write_text('bad')"
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


def test_timeout_kills_worker_process_group_and_preserves_existing_files(tmp_path: Path):
    harness = TimeoutHarness()
    existing = tmp_path / "existing.txt"
    existing.write_text("user data", encoding="utf-8")

    result = harness._invoke_local_agent_sync("codex", "task", workspace=tmp_path)
    time.sleep(2.3)

    assert result["timed_out"] is True
    assert result["exit_code"] == 124
    assert not (tmp_path / "child-survived.txt").exists()
    assert existing.read_text(encoding="utf-8") == "user data"


async def test_task_cancellation_kills_worker_process_group(tmp_path: Path):
    harness = TimeoutHarness()
    harness.config.local_agent_timeout_sec = 30
    task = asyncio.create_task(
        harness._invoke_local_agent_streaming("codex", "task", workspace=tmp_path)
    )
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(2.3)
    assert not (tmp_path / "child-survived.txt").exists()
