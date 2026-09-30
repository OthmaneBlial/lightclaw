from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.artifacts import ArtifactError
from core.bot import LightClawBot
from core.jobs import JobStore


def test_real_delegation_path_emits_private_structured_receipt(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-receipt-fixture")
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(
        workspace_path=str(tmp_path / "workspace"),
        local_agent_timeout_sec=30,
        local_agent_progress_interval_sec=10,
        local_agent_capability_profile="workspace-write",
    )
    bot._available_local_agents = lambda: {"codex": "/fixture/codex"}
    bot._delegation_safety_block_reason = lambda _task: ""
    bot.jobs = JobStore(tmp_path / "jobs.db")

    async def fake_invoke(**kwargs):
        workspace = Path(kwargs["workspace"])
        (workspace / "result.txt").write_text("verified\n", encoding="utf-8")
        return {
            "ok": True,
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "summary": "Created a verified result without sk-receipt-fixture",
            "elapsed": 0.25,
            "timed_out": False,
        }

    bot._invoke_local_agent_streaming = fake_invoke
    evidence: dict[str, object] = {}
    try:
        with caplog.at_level(logging.WARNING):
            result = asyncio.run(
                bot._run_local_agent_task(
                    session_id="fixture-session",
                    agent="codex",
                    task="Create result.txt; credential=sk-receipt-fixture",
                    evidence_sink=evidence,
                )
            )
        durable_job = bot.jobs.get_job(str(evidence["run_id"]))
    finally:
        bot.jobs.close()

    receipt_path = Path(str(evidence["receipt_json"]))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["disposition"] == "ready_for_review"
    assert receipt["plan"][0]["worker"] == "codex"
    assert receipt["commands"][0]["exit_code"] == 0
    assert receipt["file_changes"][0]["path"] == "result.txt"
    assert receipt["file_changes"][0]["change"] == "created"
    assert receipt["checks"][0]["passed"] is True
    assert receipt["checkpoint"]["type"] == "git-checkpoint"
    assert receipt["checkpoint"]["branch"].startswith("lightclaw/run-")
    assert any(str(path).endswith("changes.patch") for path in receipt["artifacts"])
    assert durable_job["status"] == "succeeded"
    assert durable_job["lanes"][0]["status"] == "succeeded"
    assert "Could not finalize durable run" not in caplog.text
    assert "[REDACTED]" in receipt["original_goal"]
    assert "sk-receipt-fixture" not in receipt_path.read_text(encoding="utf-8")
    assert "Receipt:" in result


def test_patch_failure_marks_single_agent_job_failed(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(
        workspace_path=str(root),
        local_agent_timeout_sec=30,
        local_agent_progress_interval_sec=10,
        local_agent_capability_profile="workspace-write",
    )
    bot._available_local_agents = lambda: {"codex": "/fixture/codex"}
    bot._delegation_safety_block_reason = lambda _task: ""
    bot.jobs = JobStore(tmp_path / "jobs.db")

    async def successful_invoke(**kwargs):
        (Path(kwargs["workspace"]) / "result.txt").write_text("done\n")
        return {"ok": True, "exit_code": 0, "stdout": "", "stderr": ""}

    def fail_patch(*_args, **_kwargs):
        raise ArtifactError("fixture patch failure")

    bot._invoke_local_agent_streaming = successful_invoke
    monkeypatch.setattr(
        "core.bot.delegation.workspace.initialize_artifact_repository",
        lambda *_args, **_kwargs: {"type": "fixture-checkpoint"},
    )
    monkeypatch.setattr("core.bot.delegation.execution.create_patch_bundle", fail_patch)
    evidence: dict[str, object] = {}
    try:
        asyncio.run(
            bot._run_local_agent_task(
                "fixture-session", "codex", "create result.txt", evidence_sink=evidence
            )
        )
        job = bot.jobs.get_job(str(evidence["run_id"]))
    finally:
        bot.jobs.close()

    receipt = json.loads(Path(str(evidence["receipt_json"])).read_text())
    assert job["status"] == "failed"
    assert job["lanes"][0]["status"] == "failed"
    assert receipt["disposition"] == "failed"
    assert receipt["checks"][1]["passed"] is False
    assert receipt["checks"][2]["passed"] is True


@pytest.mark.asyncio
async def test_cancel_during_single_run_job_creation_cancels_durable_row(
    tmp_path, monkeypatch
):
    root = tmp_path / "workspace"
    root.mkdir()
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(
        workspace_path=str(root),
        local_agent_timeout_sec=30,
        local_agent_progress_interval_sec=10,
        local_agent_capability_profile="workspace-write",
    )
    bot._available_local_agents = lambda: {"codex": "/fixture/codex"}
    bot._delegation_safety_block_reason = lambda _task: ""
    bot.jobs = JobStore(tmp_path / "jobs.db")
    started = threading.Event()
    release = threading.Event()
    create_job = bot.jobs.create_job

    def delayed_create_job(*args, **kwargs):
        job = create_job(*args, **kwargs)
        started.set()
        assert release.wait(timeout=5)
        return job

    monkeypatch.setattr(bot.jobs, "create_job", delayed_create_job)
    monkeypatch.setattr(
        "core.bot.delegation.workspace.initialize_artifact_repository",
        lambda *_args, **_kwargs: {"type": "fixture-checkpoint"},
    )
    bot._invoke_local_agent_streaming = AsyncMock()
    execution = asyncio.create_task(
        bot._run_local_agent_task("456", "codex", "cancel during durable setup")
    )
    try:
        assert await asyncio.to_thread(started.wait, 5)
        execution.cancel()
        await asyncio.sleep(0)
        release.set()

        with pytest.raises(asyncio.CancelledError):
            await execution

        jobs = bot.jobs.list_jobs()
        assert len(jobs) == 1
        assert jobs[0]["status"] == "canceled"
        assert jobs[0]["lanes"][0]["status"] == "canceled"
        assert Path(str(jobs[0]["workspace"])).is_dir()
        bot._invoke_local_agent_streaming.assert_not_awaited()
        assert bot._active_run_ids_by_session == {}
        assert bot._active_run_tasks_by_session == {}
    finally:
        release.set()
        bot.jobs.close()


@pytest.mark.asyncio
async def test_cancel_during_single_run_start_notice_cancels_job_and_heartbeat(
    tmp_path, monkeypatch
):
    root = tmp_path / "workspace"
    root.mkdir()
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(
        workspace_path=str(root),
        local_agent_timeout_sec=30,
        local_agent_progress_interval_sec=10,
        local_agent_capability_profile="workspace-write",
    )
    bot._available_local_agents = lambda: {"codex": "/fixture/codex"}
    bot._delegation_safety_block_reason = lambda _task: ""
    bot.jobs = JobStore(tmp_path / "jobs.db")
    bot._invoke_local_agent_streaming = AsyncMock()
    notice_started = asyncio.Event()
    never = asyncio.Event()

    async def hold_start_notice(_text):
        notice_started.set()
        await never.wait()

    monkeypatch.setattr(
        "core.bot.delegation.workspace.initialize_artifact_repository",
        lambda *_args, **_kwargs: {"type": "fixture-checkpoint"},
    )
    execution = asyncio.create_task(
        bot._run_local_agent_task(
            "456", "codex", "cancel before local process start", progress_cb=hold_start_notice
        )
    )
    try:
        await asyncio.wait_for(notice_started.wait(), timeout=5)
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution

        job = bot.jobs.list_jobs()[0]
        assert job["status"] == "canceled"
        assert job["lanes"][0]["status"] == "canceled"
        bot._invoke_local_agent_streaming.assert_not_awaited()
        assert bot._active_run_ids_by_session == {}
        assert bot._active_run_tasks_by_session == {}
    finally:
        bot.jobs.close()


@pytest.mark.asyncio
async def test_sqlite_error_after_single_run_claim_cancels_durable_job(
    tmp_path, monkeypatch
):
    root = tmp_path / "workspace"
    root.mkdir()
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(
        workspace_path=str(root),
        local_agent_timeout_sec=30,
        local_agent_progress_interval_sec=10,
        local_agent_capability_profile="workspace-write",
    )
    bot._available_local_agents = lambda: {"codex": "/fixture/codex"}
    bot._delegation_safety_block_reason = lambda _task: ""
    bot.jobs = JobStore(tmp_path / "jobs.db")
    update_lane = bot.jobs.update_lane
    fail_once = True

    def fail_starting_lane_once(*args, **kwargs):
        nonlocal fail_once
        if fail_once and args[2] == "running":
            fail_once = False
            raise sqlite3.OperationalError("database is locked")
        return update_lane(*args, **kwargs)

    monkeypatch.setattr(bot.jobs, "update_lane", fail_starting_lane_once)
    monkeypatch.setattr(
        "core.bot.delegation.workspace.initialize_artifact_repository",
        lambda *_args, **_kwargs: {"type": "fixture-checkpoint"},
    )
    bot._invoke_local_agent_streaming = AsyncMock()
    try:
        result = await bot._run_local_agent_task(
            "456", "codex", "recover from durable setup failure"
        )

        job = bot.jobs.list_jobs()[0]
        assert "local agent was not started" in result
        assert job["status"] == "canceled"
        assert job["lanes"][0]["status"] == "canceled"
        assert Path(str(job["workspace"])).is_dir()
        bot._invoke_local_agent_streaming.assert_not_awaited()
        assert bot._active_run_ids_by_session == {}
        assert bot._active_run_tasks_by_session == {}
    finally:
        bot.jobs.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failures_before_success", [1, 2])
async def test_sqlite_error_after_agent_completion_retries_and_writes_receipt(
    tmp_path, monkeypatch, caplog, failures_before_success
):
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(
        workspace_path=str(tmp_path / "workspace"),
        local_agent_timeout_sec=30,
        local_agent_progress_interval_sec=10,
        local_agent_capability_profile="workspace-write",
    )
    bot._available_local_agents = lambda: {"codex": "/fixture/codex"}
    bot._delegation_safety_block_reason = lambda _task: ""
    bot.jobs = JobStore(tmp_path / "jobs.db")
    update_lane = bot.jobs.update_lane
    failures_remaining = failures_before_success

    def fail_completion_once(*args, **kwargs):
        nonlocal failures_remaining
        if failures_remaining and args[2] == "succeeded":
            failures_remaining -= 1
            raise sqlite3.OperationalError("database is locked")
        return update_lane(*args, **kwargs)

    async def fake_invoke(**kwargs):
        (Path(kwargs["workspace"]) / "result.txt").write_text("verified\n", encoding="utf-8")
        return {
            "ok": True,
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "summary": "Created verified result",
            "elapsed": 0.25,
            "timed_out": False,
        }

    monkeypatch.setattr(bot.jobs, "update_lane", fail_completion_once)
    bot._invoke_local_agent_streaming = fake_invoke
    evidence: dict[str, object] = {}
    try:
        with caplog.at_level(logging.WARNING):
            result = await bot._run_local_agent_task(
                "fixture-session", "codex", "Create and verify result.txt", evidence_sink=evidence
            )
        job = bot.jobs.get_job(str(evidence["run_id"]))
    finally:
        bot.jobs.close()

    if failures_before_success == 1:
        assert job["status"] == "succeeded"
        assert job["lanes"][0]["status"] == "succeeded"
        assert evidence["disposition"] == "ready_for_review"
        assert evidence["checks"][-1]["passed"] is True
        assert "Local job history could not record" not in result
    else:
        assert job["status"] == "running"
        assert job["lanes"][0]["status"] == "running"
        assert evidence["disposition"] == "failed"
        assert evidence["checks"][-1]["passed"] is False
        assert "Local job history could not record" in result
    assert "Receipt:" in result
    assert "Retrying durable run finalization after SQLite error" in caplog.text


@pytest.mark.asyncio
async def test_sqlite_heartbeat_error_does_not_discard_completed_run(
    tmp_path, monkeypatch, caplog
):
    import core.bot.delegation.execution as execution

    real_sleep = asyncio.sleep

    async def quick_sleep(_delay):
        await real_sleep(0.005)

    monkeypatch.setattr(execution.asyncio, "sleep", quick_sleep)
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(
        workspace_path=str(tmp_path / "workspace"),
        local_agent_timeout_sec=30,
        local_agent_progress_interval_sec=10,
        local_agent_capability_profile="workspace-write",
    )
    bot._available_local_agents = lambda: {"codex": "/fixture/codex"}
    bot._delegation_safety_block_reason = lambda _task: ""
    bot.jobs = JobStore(tmp_path / "jobs.db")
    heartbeat = bot.jobs.heartbeat
    heartbeat_calls = 0

    def fail_first_heartbeat(*args, **kwargs):
        nonlocal heartbeat_calls
        heartbeat_calls += 1
        if heartbeat_calls == 1:
            raise sqlite3.OperationalError("database is locked")
        return heartbeat(*args, **kwargs)

    def fake_invoke(**kwargs):
        async def complete():
            await real_sleep(0.04)
            (Path(kwargs["workspace"]) / "result.txt").write_text("verified\n", encoding="utf-8")
            return {
                "ok": True,
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "summary": "Created verified result",
                "elapsed": 0.25,
                "timed_out": False,
            }

        return complete()

    monkeypatch.setattr(bot.jobs, "heartbeat", fail_first_heartbeat)
    bot._invoke_local_agent_streaming = fake_invoke
    evidence: dict[str, object] = {}
    try:
        with caplog.at_level(logging.WARNING):
            result = await bot._run_local_agent_task(
                "fixture-session", "codex", "Create and verify result.txt", evidence_sink=evidence
            )
        job = bot.jobs.get_job(str(evidence["run_id"]))
    finally:
        bot.jobs.close()

    assert heartbeat_calls >= 2
    assert job["status"] == "succeeded"
    assert evidence["disposition"] == "ready_for_review"
    assert "Receipt:" in result
    assert "Durable job heartbeat failed; retrying" in caplog.text


@pytest.mark.asyncio
async def test_canceled_run_retries_sqlite_error_when_releasing_job(
    tmp_path, monkeypatch, caplog
):
    root = tmp_path / "workspace"
    root.mkdir()
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(
        workspace_path=str(root),
        local_agent_timeout_sec=30,
        local_agent_progress_interval_sec=10,
        local_agent_capability_profile="workspace-write",
    )
    bot._available_local_agents = lambda: {"codex": "/fixture/codex"}
    bot._delegation_safety_block_reason = lambda _task: ""
    bot.jobs = JobStore(tmp_path / "jobs.db")
    update_lane = bot.jobs.update_lane
    fail_once = True
    invoke_started = asyncio.Event()
    never = asyncio.Event()

    def fail_cancellation_once(*args, **kwargs):
        nonlocal fail_once
        if fail_once and args[2] == "canceled":
            fail_once = False
            raise sqlite3.OperationalError("database is locked")
        return update_lane(*args, **kwargs)

    async def hold_agent(**_kwargs):
        invoke_started.set()
        await never.wait()

    monkeypatch.setattr(bot.jobs, "update_lane", fail_cancellation_once)
    monkeypatch.setattr(
        "core.bot.delegation.workspace.initialize_artifact_repository",
        lambda *_args, **_kwargs: {"type": "fixture-checkpoint"},
    )
    bot._invoke_local_agent_streaming = hold_agent
    task = asyncio.create_task(
        bot._run_local_agent_task("fixture-session", "codex", "cancel active work")
    )
    try:
        await asyncio.wait_for(invoke_started.wait(), timeout=5)
        task.cancel()
        with caplog.at_level(logging.WARNING), pytest.raises(asyncio.CancelledError):
            await task

        job = bot.jobs.list_jobs()[0]
        assert job["status"] == "canceled"
        assert job["lanes"][0]["status"] == "canceled"
        assert Path(str(job["workspace"])).is_dir()
        assert "Retrying durable job cancellation after SQLite error" in caplog.text
    finally:
        bot.jobs.close()


@pytest.mark.asyncio
async def test_cancel_during_post_run_snapshot_cancels_durable_job(
    tmp_path, monkeypatch
):
    root = tmp_path / "workspace"
    root.mkdir()
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(
        workspace_path=str(root),
        local_agent_timeout_sec=30,
        local_agent_progress_interval_sec=10,
        local_agent_capability_profile="workspace-write",
    )
    bot._available_local_agents = lambda: {"codex": "/fixture/codex"}
    bot._delegation_safety_block_reason = lambda _task: ""
    bot.jobs = JobStore(tmp_path / "jobs.db")
    snapshot_count = 0
    snapshot_started = threading.Event()
    release_snapshot = threading.Event()

    def snapshot(_workspace):
        nonlocal snapshot_count
        snapshot_count += 1
        if snapshot_count == 2:
            snapshot_started.set()
            assert release_snapshot.wait(timeout=5)
        return {}

    monkeypatch.setattr(bot, "_snapshot_workspace_state", snapshot, raising=False)
    monkeypatch.setattr(
        "core.bot.delegation.workspace.initialize_artifact_repository",
        lambda *_args, **_kwargs: {"type": "fixture-checkpoint"},
    )
    bot._invoke_local_agent_streaming = AsyncMock(
        return_value={
            "ok": True,
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "summary": "Agent completed",
            "elapsed": 0.25,
            "timed_out": False,
        }
    )
    task = asyncio.create_task(
        bot._run_local_agent_task("fixture-session", "codex", "cancel during final snapshot")
    )
    try:
        assert await asyncio.to_thread(snapshot_started.wait, 5)
        task.cancel()
        release_snapshot.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        job = bot.jobs.list_jobs()[0]
        assert job["status"] == "canceled"
        assert job["lanes"][0]["status"] == "canceled"
        assert Path(str(job["workspace"])).is_dir()
        assert bot._active_run_ids_by_session == {}
        assert bot._active_run_tasks_by_session == {}
    finally:
        release_snapshot.set()
        bot.jobs.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failures_before_success", [1, 2])
async def test_durable_job_finish_retries_and_records_sqlite_failure(
    tmp_path, monkeypatch, failures_before_success
):
    bot = LightClawBot.__new__(LightClawBot)
    bot.jobs = JobStore(tmp_path / "jobs.db")
    workspace = tmp_path / "repo"
    job = bot.jobs.create_job(
        workspace=workspace,
        session_id="fixture-session",
        goal="finish after a transient lock",
        approved_scope="fixture",
        risk_level="medium",
        capability_profile="workspace-write",
        plan=[
            {
                "label": "delegation",
                "depends_on": [],
                "owned_paths": [],
                "idempotent": False,
                "resumable": False,
                "max_attempts": 1,
            }
        ],
        status="queued",
        run_id="finish-retry",
    )
    bot.jobs.claim_next(workspace=workspace)
    bot.jobs.update_lane(job["run_id"], "delegation", "succeeded")
    finish = bot.jobs.finish
    failures_remaining = failures_before_success

    def fail_finish_once(*args, **kwargs):
        nonlocal failures_remaining
        if failures_remaining:
            failures_remaining -= 1
            raise sqlite3.OperationalError("database is locked")
        return finish(*args, **kwargs)

    monkeypatch.setattr(bot.jobs, "finish", fail_finish_once)
    try:
        checks: list[dict[str, object]] = []
        failures: list[str] = []
        lines: list[str] = []
        await bot._record_multi_job_finalization(
            str(job["run_id"]), checks, failures, lines
        )
        if failures_before_success == 1:
            assert bot.jobs.get_job(str(job["run_id"]))["status"] == "succeeded"
            assert checks[0]["passed"] is True
            assert failures == []
            assert lines == []
        else:
            assert bot.jobs.get_job(str(job["run_id"]))["status"] == "running"
            assert checks[0]["passed"] is False
            assert failures == ["durable job finalization failed: OperationalError"]
            assert lines and "receipt is marked failed" in lines[0]
    finally:
        bot.jobs.close()
