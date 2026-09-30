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


def test_acceptance_command_uses_secret_free_minimal_environment(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-secret")
    monkeypatch.setenv("LIGHTCLAW_TEST_SECRET", "provider-secret")
    launched = {}

    async def run(*argv, **kwargs):
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
        launched["argv"] = argv
        launched["kwargs"] = kwargs
        return process

    monkeypatch.setattr(
        "core.bot.commands.agent_acceptance.asyncio.create_subprocess_exec", run
    )
    bot = LightClawBot.__new__(LightClawBot)

    failure = bot._run_multi_acceptance_command(
        workspace, {"command": "python -c pass"}
    )

    assert failure == ""
    child_env = launched["kwargs"]["env"]
    assert "TELEGRAM_BOT_TOKEN" not in child_env
    assert "LIGHTCLAW_TEST_SECRET" not in child_env
    assert child_env["LIGHTCLAW_DELEGATED"] == "1"
    assert child_env["CI"] == "1"
    assert launched["kwargs"]["start_new_session"] is (os.name == "posix")


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
