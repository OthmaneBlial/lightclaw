from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.bot.delegation.agents import DelegationAgentsMixin
from core.bot.delegation.execution import DelegationExecutionMixin
from core.bot.delegation.streams import BoundedStreamCapture
from core.bot.delegation.workspace import DelegationWorkspaceMixin
from core.jobs import JobStore
from core.workspaces import WorkspaceSafetyError


class ExecutionHarness(DelegationExecutionMixin, DelegationWorkspaceMixin):
    def __init__(self):
        self.config = SimpleNamespace(
            local_agent_capability_profile="workspace-write",
            local_agent_progress_interval_sec=30,
        )


class AgentLookupHarness(DelegationAgentsMixin):
    pass


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


def test_agent_lookup_ignores_relative_path_components(monkeypatch):
    seen_paths = []
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join((".", "/usr/bin", "bin", "")),
    )

    def which(_binary: str, *, path: str):
        seen_paths.append(path)
        return "/opt/tools/codex" if _binary == "codex" else None

    monkeypatch.setattr("core.bot.delegation.agents.shutil.which", which)

    assert AgentLookupHarness()._available_local_agents() == {
        "codex": "/opt/tools/codex"
    }
    assert seen_paths == ["/usr/bin", "/usr/bin"]


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


def test_progress_summary_redacts_agent_emitted_credentials():
    harness = ExecutionHarness()
    state = harness._new_progress_state()
    secret = "sk-test-secret-value"
    state["last_activity"] = f"command output: OPENAI_API_KEY={secret}"

    rendered = harness._render_progress_summary("codex", state, 1, heartbeat=False)

    assert secret not in rendered
    assert "[REDACTED]" in rendered


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


@pytest.mark.parametrize("agent", ["codex", "claude"])
@pytest.mark.parametrize("phase", ["before_open", "launch"])
async def test_agent_startup_cannot_follow_replaced_workspace(tmp_path, monkeypatch, agent, phase):
    workspace = tmp_path / "checked"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    fake_agent = tmp_path / "fixture-agent"
    fake_agent.write_text(
        f"#!{sys.executable}\n"
        "import os,sys,json\nfrom pathlib import Path\n"
        "prompt = sys.stdin.read()\n"
        "if '-C' in sys.argv: os.chdir(sys.argv[sys.argv.index('-C')+1])\n"
        "Path('marker').write_text(prompt)\n"
        "print(json.dumps({'type':'result','result':'fixture finished','is_error':False}))\n"
    )
    fake_agent.chmod(0o700)
    harness = TimeoutHarness()
    harness.config.local_agent_timeout_sec = 10
    harness._build_delegation_prompt = lambda task, workspace=None: (
        DelegationWorkspaceMixin._build_delegation_prompt(harness, task, workspace=workspace)
    )
    original_launch = asyncio.create_subprocess_exec

    def swap():
        workspace.rename(tmp_path / "original")
        workspace.symlink_to(outside, target_is_directory=True)

    def build(**kwargs):
        command, prompt = ExecutionHarness._build_local_agent_command(harness, **kwargs)
        command[0] = str(fake_agent)
        if phase == "before_open":
            swap()
        return command, prompt

    async def launch(*args, **kwargs):
        if phase == "launch":
            swap()
        return await original_launch(*args, **kwargs)

    harness._build_local_agent_command = build
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    result = await harness._invoke_local_agent_streaming(agent, "fixture", workspace=workspace)

    assert not (outside / "marker").exists()
    if phase == "launch":
        assert result["ok"] is True
        prompt = (tmp_path / "original" / "marker").read_text()
        assert "Workspace root: current working directory (.)" in prompt
        assert "fixture" in prompt
    else:
        assert result["ok"] is False
        assert not (tmp_path / "original" / "marker").exists()


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


async def test_prompt_stdin_failure_is_not_reported_as_success(tmp_path: Path):
    harness = TimeoutHarness()
    harness._build_local_agent_command = lambda **_kwargs: (
        [sys.executable, "-c", "pass"],
        "x" * (4 * 1024 * 1024),
    )

    result = await harness._invoke_local_agent_streaming(
        "codex", "task", workspace=tmp_path
    )

    assert result["ok"] is False
    assert result["exit_code"] == 1
    assert "Prompt stdin failed" in result["stderr"]


async def test_stream_read_failure_terminates_worker_process(tmp_path: Path, monkeypatch):
    harness = TimeoutHarness()
    harness.config.local_agent_timeout_sec = 5
    survived = tmp_path / "worker-survived.txt"
    harness._build_local_agent_command = lambda **_kwargs: (
        [
            sys.executable,
            "-c",
            "import pathlib,time; time.sleep(.3); "
            f"pathlib.Path({str(survived)!r}).touch()",
        ],
        None,
    )

    async def fail_read(_self, _stream):
        if False:
            yield ""
        raise OSError("fixture stream failure")

    monkeypatch.setattr(BoundedStreamCapture, "read_lines", fail_read)
    try:
        result = await harness._invoke_local_agent_streaming(
            "codex", "task", workspace=tmp_path
        )
    except OSError:
        result = None
    await asyncio.sleep(.4)

    assert result is not None
    assert result["ok"] is False
    assert not survived.exists()


@pytest.mark.parametrize("cancel_count", [1, 3])
async def test_task_cancellation_kills_term_resistant_worker_process_group(
    tmp_path: Path, monkeypatch, cancel_count
):
    harness = TimeoutHarness(resistant_child=True)
    harness.config.local_agent_timeout_sec = 30
    term_sent = asyncio.Event()
    process_group = None
    killpg = os.killpg

    def record_signal(pid, sig):
        nonlocal process_group
        killpg(pid, sig)
        process_group = pid
        if sig == signal.SIGTERM:
            term_sent.set()

    monkeypatch.setattr("core.bot.delegation.execution.os.killpg", record_signal)
    task = asyncio.create_task(
        harness._invoke_local_agent_streaming("codex", "task", workspace=tmp_path)
    )
    try:
        for _ in range(100):
            if (tmp_path / "child-ready.txt").exists():
                break
            await asyncio.sleep(0.02)
        assert (tmp_path / "child-ready.txt").exists()
        task.cancel()
        await asyncio.wait_for(term_sent.wait(), timeout=2)
        for _ in range(cancel_count - 1):
            task.cancel()
            await asyncio.sleep(.02)
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(2.3)
        assert not (tmp_path / "child-survived.txt").exists()
    finally:
        if process_group is not None:
            try:
                killpg(process_group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("cancel_count", [1, 3])
async def test_cancellation_during_process_registration_kills_and_unregisters_worker(
    tmp_path: Path, cancel_count,
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

        for _ in range(cancel_count):
            task.cancel()
            await asyncio.sleep(.02)
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


@pytest.mark.parametrize("cancel_count", [1, 3])
async def test_cancellation_during_startup_kills_term_resistant_child(
    tmp_path: Path, monkeypatch, cancel_count
):
    harness = TimeoutHarness(resistant_child=True)
    harness.config.local_agent_timeout_sec = 30
    command, _ = harness._build_local_agent_command("codex", tmp_path, "task", True)
    command[2] = (
        "import os,pathlib; pathlib.Path('startup-pid.txt').write_text(str(os.getpid())); "
        + command[2]
    ).replace("time.sleep(30)", "os.write(1, b'x'*262144); time.sleep(30)")
    harness._build_local_agent_command = lambda **_kwargs: (command, None)
    create_process = asyncio.create_subprocess_exec
    process = None

    async def capture_process(*args, **kwargs):
        nonlocal process
        process = await create_process(*args, **kwargs)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_process)
    loop = asyncio.get_running_loop()
    connect_read_pipe = loop.connect_read_pipe
    connecting = asyncio.Event()
    release = asyncio.Event()

    async def delayed_connection(*args, **kwargs):
        connecting.set()
        await release.wait()
        return await connect_read_pipe(*args, **kwargs)

    monkeypatch.setattr(loop, "connect_read_pipe", delayed_connection)
    task = asyncio.create_task(
        harness._invoke_local_agent_streaming("codex", "task", workspace=tmp_path)
    )
    try:
        await asyncio.wait_for(connecting.wait(), timeout=2)
        for _ in range(100):
            if (tmp_path / "child-ready.txt").exists():
                break
            await asyncio.sleep(.02)
        assert (tmp_path / "child-ready.txt").exists()
        for _ in range(cancel_count):
            task.cancel()
            await asyncio.sleep(0)
        release.set()
        _, pending = await asyncio.wait([task], timeout=2)
        assert not pending, "startup cancellation blocked on undrained output"
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.stdout.at_eof()
        assert process.stderr.at_eof()
        await asyncio.sleep(2.3)
        assert not (tmp_path / "child-survived.txt").exists()
    finally:
        release.set()
        pid_path = tmp_path / "startup-pid.txt"
        if pid_path.exists():
            try:
                os.killpg(int(pid_path.read_text()), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        if not task.done():
            task.cancel()
        if process is not None:
            async def drain(stream):
                while await stream.read(65536):
                    pass

            await asyncio.gather(
                drain(process.stdout), drain(process.stderr), return_exceptions=True
            )
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


async def test_failed_process_registration_kills_immediately(tmp_path: Path, monkeypatch):
    harness = TimeoutHarness()
    signals = []
    killpg = os.killpg

    def record_signal(pid, sig):
        signals.append(sig)
        killpg(pid, sig)

    def reject_registration(*_args):
        raise RuntimeError("registration fixture failure")

    monkeypatch.setattr("core.bot.delegation.execution.os.killpg", record_signal)
    harness.jobs = SimpleNamespace(register_process_group=reject_registration)
    harness._build_local_agent_command = lambda **_kwargs: (
        [sys.executable, "-c", "import time; time.sleep(30)"], None
    )

    result = await asyncio.wait_for(
        harness._invoke_local_agent_streaming(
            "codex", "task", workspace=tmp_path, job_run_id="refused"
        ),
        timeout=2,
    )

    assert result["ok"] is False
    assert "could not register delegated process group" in result["stderr"]
    assert signals == [signal.SIGKILL]


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
