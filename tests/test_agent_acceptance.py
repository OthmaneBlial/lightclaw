from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from core.bot import LightClawBot
from core.jobs import JobStore


def _acceptance(bot: LightClawBot, workspace):
    return bot._evaluate_multi_worker_acceptance(
        workspace,
        "builder",
        {
            "role": "implementation",
            "owned_paths": ["README.md"],
            "acceptance_checks": [
                {"type": "handoff_json", "path": "handoff/builder.json"},
                {"type": "json_field_nonempty", "field": "summary"},
            ],
        },
    )


def test_contract_path_normalization_rejects_nul_bytes():
    bot = LightClawBot.__new__(LightClawBot)
    assert bot._normalize_multi_contract_path("handoff/bad\x00.json") == ""


def test_handoff_acceptance_rejects_json_over_one_mib(tmp_path):
    handoff = tmp_path / "handoff" / "builder.json"
    handoff.parent.mkdir()
    handoff.write_bytes(
        b'{"lane":"builder","summary":"'
        + b"x" * (1024 * 1024)
        + b'","changed_files":[]}'
    )
    bot = LightClawBot.__new__(LightClawBot)

    passed, failures, _ = _acceptance(bot, tmp_path)

    assert not passed
    assert any("exceeds 1 MiB" in failure for failure in failures)


def test_handoff_acceptance_rejects_symlinked_json(tmp_path):
    handoff = tmp_path / "handoff" / "builder.json"
    handoff.parent.mkdir()
    secret_json = tmp_path / "outside.json"
    secret_json.write_text(
        json.dumps({"lane": "builder", "summary": "outside", "changed_files": []}),
        encoding="utf-8",
    )
    handoff.symlink_to(secret_json)
    bot = LightClawBot.__new__(LightClawBot)

    passed, failures, _ = _acceptance(bot, tmp_path)

    assert not passed
    assert any("symlink" in failure for failure in failures)


def test_acceptance_command_rejects_cwd_symlink_outside_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "external").symlink_to(outside, target_is_directory=True)
    async def run(*_args, **_kwargs):
        raise AssertionError("an outside cwd must not start a process")

    monkeypatch.setattr(
        "core.bot.commands.agent_acceptance.asyncio.create_subprocess_exec", run
    )
    bot = LightClawBot.__new__(LightClawBot)

    failure = bot._run_multi_acceptance_command(
        workspace, {"command": "python -c pass", "cwd": "external"}
    )

    assert "outside the workspace" in failure


@pytest.mark.parametrize("phase", ["validation", "launch"])
def test_acceptance_command_cwd_cannot_be_redirected_at_launch(tmp_path, monkeypatch, phase):
    workspace = tmp_path / "workspace"
    target = workspace / "checked"
    target.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    bot = LightClawBot.__new__(LightClawBot)
    original_resolve = bot._resolve_multi_workspace_path
    original_launch = asyncio.create_subprocess_exec
    passed_fds = []

    def swap():
        target.rename(workspace / "original")
        target.symlink_to(outside, target_is_directory=True)

    def resolve(*args):
        path = original_resolve(*args)
        if phase == "validation":
            swap()
        return path

    async def launch(*args, **kwargs):
        passed_fds.extend(kwargs.get("pass_fds", ()))
        if phase == "launch":
            swap()
        return await original_launch(*args, **kwargs)

    monkeypatch.setattr(bot, "_resolve_multi_workspace_path", resolve)
    monkeypatch.setattr("core.bot.commands.agent_acceptance.asyncio.create_subprocess_exec", launch)
    command = shlex.join([
        sys.executable, "-c", "from pathlib import Path; Path('marker').write_text('executed')"
    ])
    failure = bot._run_multi_acceptance_command(workspace, {"command": command, "cwd": "checked"})

    assert not (outside / "marker").exists()
    if phase == "launch":
        assert failure == ""
        assert (workspace / "original" / "marker").exists()
        assert passed_fds
        for fd in passed_fds:
            with pytest.raises(OSError):
                os.fstat(fd)
    else:
        assert failure
        assert not (workspace / "original" / "marker").exists()


@pytest.mark.parametrize("phase", ["existing", "validation", "launch"])
@pytest.mark.parametrize("parent_alias", [False, True])
def test_acceptance_keeps_requested_root_boundary(tmp_path, monkeypatch, phase, parent_alias):
    actual_parent = tmp_path / "actual"
    actual_parent.mkdir()
    parent = tmp_path / "alias" if parent_alias else actual_parent
    if parent_alias:
        parent.symlink_to(actual_parent, target_is_directory=True)
    workspace = parent / "workspace"
    (workspace / "checked").mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "checked").mkdir(parents=True)
    (outside / "checked" / "proof.txt").write_text("outside proof")
    original = tmp_path / "original-workspace"
    bot = LightClawBot.__new__(LightClawBot)
    resolve = bot._resolve_multi_workspace_path
    launch = asyncio.create_subprocess_exec

    def swap():
        workspace.rename(original)
        workspace.symlink_to(outside, target_is_directory=True)

    def resolve_with_swap(*args):
        if phase == "validation" and not workspace.is_symlink():
            swap()
        return resolve(*args)

    async def launch_with_swap(*args, **kwargs):
        if phase == "launch":
            swap()
        return await launch(*args, **kwargs)

    if phase == "existing":
        swap()
    monkeypatch.setattr(bot, "_resolve_multi_workspace_path", resolve_with_swap)
    monkeypatch.setattr("core.bot.commands.agent_acceptance.asyncio.create_subprocess_exec", launch_with_swap)
    command = shlex.join([sys.executable, "-c", "from pathlib import Path; Path('marker').write_text('executed')"])

    failure = bot._run_multi_acceptance_command(workspace, {"command": command, "cwd": "checked"})

    assert not (outside / "checked" / "marker").exists()
    assert not bot._multi_workspace_path_exists(workspace, "checked/proof.txt", file_only=True)
    assert (outside / "checked" / "proof.txt").read_text() == "outside proof"
    assert bool(failure) is (phase != "launch")
    assert (original / "checked" / "marker").exists() is (phase == "launch")


@pytest.mark.parametrize("launch_failure", [False, True])
def test_acceptance_command_uses_secret_free_minimal_environment_and_closes_cwd(
    tmp_path, monkeypatch, launch_failure
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-secret")
    monkeypatch.setenv("LIGHTCLAW_TEST_SECRET", "provider-secret")
    launched = {}

    async def run(*argv, **kwargs):
        launched["argv"] = argv
        launched["kwargs"] = kwargs
        if launch_failure:
            raise OSError("fixture launch failure")
        stdout = asyncio.StreamReader()
        stderr = asyncio.StreamReader()
        stdout.feed_eof()
        stderr.feed_eof()
        process = SimpleNamespace(
            pid=123, stdout=stdout, stderr=stderr, returncode=None
        )

        async def wait():
            process.returncode = 0
            return 0

        process.wait = wait
        return process

    monkeypatch.setattr(
        "core.bot.commands.agent_acceptance.asyncio.create_subprocess_exec", run
    )
    bot = LightClawBot.__new__(LightClawBot)

    failure = bot._run_multi_acceptance_command(
        workspace, {"command": "python -c pass"}
    )

    assert bool(failure) is launch_failure
    if launch_failure:
        assert "fixture launch failure" in failure
    child_env = launched["kwargs"]["env"]
    assert "TELEGRAM_BOT_TOKEN" not in child_env
    assert "LIGHTCLAW_TEST_SECRET" not in child_env
    assert child_env["LIGHTCLAW_DELEGATED"] == "1"
    assert child_env["CI"] == "1"
    assert launched["kwargs"]["start_new_session"] is (os.name == "posix")
    for fd in launched["kwargs"]["pass_fds"]:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_acceptance_command_bounds_captured_output_and_keeps_error_detail(tmp_path):
    bot = LightClawBot.__new__(LightClawBot)
    command = shlex.join(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('x' * 8_000_000); "
            "sys.stderr.write('useful failure'); sys.exit(1)",
        ]
    )

    failure = bot._run_multi_acceptance_command(
        tmp_path, {"command": command, "timeout_sec": 10}
    )

    assert "acceptance command output truncated" in failure
    assert "useful failure" in failure
    assert len(failure) < 500


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="process-group cancellation requires POSIX")
async def test_acceptance_command_cancellation_kills_descendant_processes(tmp_path):
    marker = tmp_path / "canceled-command-finished"
    program = (
        "import pathlib,time;time.sleep(1.4);"
        f"pathlib.Path({str(marker)!r}).write_text('survived')"
    )
    command = shlex.join([sys.executable, "-c", program])
    bot = LightClawBot.__new__(LightClawBot)
    contract = {
        "acceptance_checks": [
            {"type": "command_succeeds", "command": command, "timeout_sec": 10}
        ]
    }

    async def run_acceptance():
        return await bot._evaluate_multi_worker_acceptance_off_thread(
            tmp_path, "builder", contract
        )

    task = asyncio.create_task(run_acceptance())
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(1.6)

    assert not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="process-group recovery requires POSIX")
def test_stalled_recovery_kills_acceptance_command_group(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    plan = [
        {
            "label": "builder",
            "depends_on": [],
            "owned_paths": ["src"],
            "idempotent": True,
            "resumable": True,
            "max_attempts": 1,
        }
    ]
    job = store.create_job(
        workspace=tmp_path,
        session_id="fixture",
        goal="acceptance recovery",
        approved_scope="src only",
        risk_level="low",
        capability_profile="workspace-write",
        plan=plan,
        status="queued",
    )
    store.claim_next(workspace=tmp_path, worker_pid=999999)
    marker = tmp_path / "acceptance-command-survived"
    command = shlex.join(
        [
            sys.executable,
            "-c",
            "import pathlib,time;time.sleep(1.5);"
            f"pathlib.Path({str(marker)!r}).write_text('survived')",
        ]
    )
    bot = LightClawBot.__new__(LightClawBot)
    bot.jobs = store
    result = {}

    def run_command():
        result["failure"] = bot._run_multi_acceptance_command(
            tmp_path,
            {"command": command, "timeout_sec": 10},
            job_run_id=str(job["run_id"]),
        )

    worker = threading.Thread(target=run_command, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 3
        registered = None
        while time.monotonic() < deadline:
            with store._lock:
                registered = store.db.execute(
                    "SELECT 1 FROM job_process_groups WHERE run_id = ?",
                    (job["run_id"],),
                ).fetchone()
            if registered:
                break
            time.sleep(0.02)
        assert registered
        assert store.recover_stalled() == [job["run_id"]]
        worker.join(timeout=3)
        assert not worker.is_alive()
        time.sleep(1.6)
        assert not marker.exists()
        assert "failure" in result
        assert store.db.execute(
            "SELECT 1 FROM job_process_groups WHERE run_id = ?", (job["run_id"],)
        ).fetchone() is None
    finally:
        store._stop_process_groups(str(job["run_id"]))
        worker.join(timeout=3)
        store.close()


@pytest.mark.skipif(os.name != "posix", reason="process-group signals require POSIX")
def test_acceptance_timeout_falls_back_when_group_signals_fail(tmp_path, monkeypatch):
    bot = LightClawBot.__new__(LightClawBot)
    signal_calls = []

    async def launch(*_args, **_kwargs):
        stdout = asyncio.StreamReader()
        stderr = asyncio.StreamReader()
        stdout.feed_eof()
        stderr.feed_eof()
        exited = asyncio.Event()
        process = SimpleNamespace(
            pid=123, returncode=None, stdin=None, stdout=stdout, stderr=stderr
        )

        async def wait():
            await exited.wait()
            return process.returncode

        def terminate():
            signal_calls.append("terminate")

        def kill():
            signal_calls.append("kill")
            process.returncode = -9
            exited.set()

        process.wait = wait
        process.terminate = terminate
        process.kill = kill
        return process

    def deny_group_signal(_pid, _signal):
        raise PermissionError("group signal unavailable")

    monkeypatch.setattr(
        "core.bot.commands.agent_acceptance.create_subprocess_at", launch
    )
    monkeypatch.setattr(
        "core.bot.delegation.workspace.os.killpg", deny_group_signal
    )

    failure = bot._run_multi_acceptance_command(
        tmp_path, {"command": "python -c pass", "timeout_sec": 1}
    )

    assert "timed out after 1s" in failure
    assert signal_calls == ["terminate", "kill"]


@pytest.mark.skipif(os.name != "posix", reason="acceptance process groups require POSIX")
def test_acceptance_timeout_kills_descendant_processes(tmp_path):
    marker = tmp_path / "orphan-finished"
    child = (
        "import pathlib,time;time.sleep(1.4);"
        f"pathlib.Path({str(marker)!r}).write_text('survived')"
    )
    parent = (
        "import subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{child!r}]);time.sleep(10)"
    )
    bot = LightClawBot.__new__(LightClawBot)

    failure = bot._run_multi_acceptance_command(
        tmp_path,
        {"command": shlex.join([sys.executable, "-c", parent]), "timeout_sec": 1},
    )

    assert "timed out" in failure
    time.sleep(1.6)
    assert not marker.exists()


def test_handoff_acceptance_rejects_symlinked_parent_outside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "builder.json").write_text(
        json.dumps({"lane": "builder", "summary": "outside", "changed_files": []}),
        encoding="utf-8",
    )
    (workspace / "handoff").symlink_to(outside, target_is_directory=True)
    bot = LightClawBot.__new__(LightClawBot)

    passed, failures, _ = _acceptance(bot, workspace)

    assert not passed
    assert failures


@pytest.mark.parametrize("operation", ["load", "acceptance"])
def test_handoff_read_rejects_parent_swapped_after_path_validation(tmp_path, monkeypatch, operation):
    import core.bot.commands.agent_acceptance as acceptance

    workspace = tmp_path / "workspace"
    parent = workspace / "handoff"
    parent.mkdir(parents=True)
    payload = {"lane": "builder", "summary": "approved", "changed_files": []}
    (parent / "builder.json").write_text(json.dumps(payload), encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "builder.json").write_text(
        json.dumps({**payload, "summary": "outside fixture"}), encoding="utf-8",
    )
    original_read = acceptance.read_json_object

    def swap_before_read(*args, **kwargs):
        parent.rename(tmp_path / "original-handoff")
        parent.symlink_to(outside, target_is_directory=True)
        return original_read(*args, **kwargs)

    monkeypatch.setattr(acceptance, "read_json_object", swap_before_read)
    bot = LightClawBot.__new__(LightClawBot)
    if operation == "load":
        data, error = bot._load_multi_worker_handoff(workspace, "builder")
        assert data == {}
        assert error
    else:
        passed, failures, _ = bot._evaluate_multi_worker_acceptance(
            workspace, "builder",
            {"acceptance_checks": [{"type": "handoff_json", "path": "handoff/builder.json"}]},
        )
        assert not passed
        assert failures


@pytest.mark.parametrize(
    "check",
    [
        {"type": "file_exists", "path": "external/secret.txt"},
        {"type": "glob_nonempty", "pattern": "external/*.txt"},
    ],
)
def test_acceptance_checks_reject_paths_through_external_symlink(tmp_path, check):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    (workspace / "external").symlink_to(outside, target_is_directory=True)
    bot = LightClawBot.__new__(LightClawBot)

    passed, failures, _ = bot._evaluate_multi_worker_acceptance(
        workspace,
        "builder",
        {"role": "implementation", "owned_paths": [], "acceptance_checks": [check]},
    )

    assert not passed
    assert failures


@pytest.mark.parametrize("kind", ["file_exists", "glob_nonempty", "reported_files_exist", "deliverable"])
@pytest.mark.parametrize("swap", ["leaf", "parent"])
def test_acceptance_existence_rejects_symlink_swap(tmp_path, monkeypatch, kind, swap):
    workspace = tmp_path / "workspace"
    parent = workspace / "result"
    parent.mkdir(parents=True)
    target = parent / "proof.txt"
    target.write_text("workspace proof")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / target.name).write_text("outside proof")
    handoff = workspace / "handoff" / "builder.json"
    handoff.parent.mkdir()
    handoff.write_text(json.dumps({
        "lane": "builder", "summary": "done", "changed_files": ["result/proof.txt"],
        "outputs": {"deliverables": ["result/proof.txt"]},
    }))
    bot = LightClawBot.__new__(LightClawBot)
    original = bot._resolve_multi_workspace_path

    def swapped_after_validation(root, relative):
        path = original(root, relative)
        if relative == "result/proof.txt":
            if swap == "parent":
                parent.rename(workspace / "original")
                parent.symlink_to(outside, target_is_directory=True)
            else:
                target.unlink()
                target.symlink_to(outside / target.name)
        return path

    monkeypatch.setattr(bot, "_resolve_multi_workspace_path", swapped_after_validation)
    if kind == "deliverable":
        audited, failures = bot._audit_multi_lane_deliverables(
            workspace, {"builder": {"role": "authoring"}}
        )
        assert audited
    else:
        check = {"type": kind}
        if kind == "file_exists":
            check["path"] = "result/proof.txt"
        elif kind == "glob_nonempty":
            check["pattern"] = "result/*.txt"
        passed, failures, _ = bot._evaluate_multi_worker_acceptance(
            workspace, "builder", {"acceptance_checks": [check]}
        )
        assert not passed
    assert failures


def test_acceptance_existence_preserves_internal_aliases_and_metadata_only_checks(tmp_path):
    directory = tmp_path / "actual"
    directory.mkdir()
    target = directory / "proof.txt"
    target.write_text("proof")
    target.chmod(0)
    (directory / "alias.txt").symlink_to(target)
    (tmp_path / "alias").symlink_to(directory, target_is_directory=True)
    bot = LightClawBot.__new__(LightClawBot)
    try:
        assert bot._multi_workspace_path_exists(tmp_path, "alias/alias.txt", file_only=True)
        assert bot._multi_workspace_path_exists(tmp_path, "alias")
        assert not bot._multi_workspace_path_exists(tmp_path, "alias", file_only=True)
        assert not bot._multi_workspace_path_exists(tmp_path, "missing")
    finally:
        target.chmod(0o600)


def test_reported_files_reject_paths_through_external_symlink(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "handoff").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    (workspace / "external").symlink_to(outside, target_is_directory=True)
    (workspace / "handoff" / "builder.json").write_text(
        json.dumps(
            {"lane": "builder", "summary": "done", "changed_files": ["external/secret.txt"]}
        ),
        encoding="utf-8",
    )
    bot = LightClawBot.__new__(LightClawBot)

    passed, failures, _ = bot._evaluate_multi_worker_acceptance(
        workspace,
        "builder",
        {
            "role": "implementation",
            "owned_paths": [],
            "acceptance_checks": [
                {"type": "handoff_json", "path": "handoff/builder.json"},
                {"type": "reported_files_exist"},
            ],
        },
    )

    assert not passed
    assert failures
