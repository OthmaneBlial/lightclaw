from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import time

import pytest

from core.jobs import JobConflictError, JobStateError, JobStore, inspect_job_database
from lightclaw_cli import build_parser


def _plan(*, overlap: bool = False, resumable: bool = True):
    return [
        {
            "label": "backend",
            "depends_on": [],
            "owned_paths": ["src/api"],
            "idempotent": True,
            "resumable": resumable,
            "max_attempts": 2,
        },
        {
            "label": "frontend",
            "depends_on": [] if overlap else ["backend"],
            "owned_paths": ["src/api/routes.py" if overlap else "src/ui"],
            "idempotent": True,
            "resumable": resumable,
            "max_attempts": 2,
        },
    ]


def _create(store: JobStore, workspace, *, priority=0, plan=None, status="queued"):
    return store.create_job(
        workspace=workspace,
        session_id="fixture",
        goal="bounded fixture goal",
        approved_scope="src only",
        risk_level="medium",
        capability_profile="workspace-write",
        plan=plan or _plan(),
        priority=priority,
        status=status,
    )


def test_jobs_persist_across_restart_with_private_database(tmp_path):
    path = tmp_path / "state" / "jobs.db"
    first = JobStore(path)
    created = _create(first, tmp_path / "repo", status="awaiting_approval")
    first.approve(created["run_id"])
    first.close()

    second = JobStore(path)
    restored = second.get_job(created["run_id"])
    assert restored["status"] == "queued"
    assert [lane["label"] for lane in restored["lanes"]] == ["backend", "frontend"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    second.close()


def test_priority_queue_and_one_active_writer_per_workspace(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    workspace = tmp_path / "repo"
    low = _create(store, workspace, priority=1)
    high = _create(store, workspace, priority=50)

    claimed = store.claim_next(workspace=workspace, worker_pid=12345)
    assert claimed["run_id"] == high["run_id"]
    assert store.claim_next(workspace=workspace, worker_pid=12346) is None
    store.finish(high["run_id"], succeeded=True)
    assert store.claim_next(workspace=workspace, worker_pid=12346)["run_id"] == low["run_id"]
    store.close()


def test_parallel_owned_path_overlap_is_rejected_but_sequential_overlap_is_allowed(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    with pytest.raises(JobConflictError, match="parallel lanes overlap"):
        _create(store, tmp_path / "repo", plan=_plan(overlap=True))

    sequential = _plan(overlap=True)
    sequential[1]["depends_on"] = ["backend"]
    created = _create(store, tmp_path / "repo", plan=sequential)
    assert created["status"] == "queued"
    store.close()


def test_cancel_resume_and_bounded_idempotent_lane_retry(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = _create(store, tmp_path / "repo")
    store.claim_next(workspace=tmp_path / "repo", worker_pid=999999)
    requested = store.request_cancel(job["run_id"])
    assert requested["status"] == "cancel_requested"
    assert store.claim_next(workspace=tmp_path / "repo") is None
    paused = store.pause(job["run_id"])
    assert paused["status"] == "paused"
    assert store.resume(job["run_id"])["status"] == "queued"

    store.claim_next(workspace=tmp_path / "repo", worker_pid=999999)
    store.update_lane(job["run_id"], "backend", "running", increment_attempt=True)
    store.update_lane(job["run_id"], "backend", "failed", error="fixture failure")
    store.finish(job["run_id"], succeeded=False, error="fixture failure")
    retried = store.retry_lane(job["run_id"], "backend")
    assert retried["status"] == "queued"
    assert retried["retry_count"] == 1

    store.claim_next(workspace=tmp_path / "repo", worker_pid=999999)
    store.update_lane(job["run_id"], "backend", "running", increment_attempt=True)
    store.update_lane(job["run_id"], "backend", "failed")
    store.finish(job["run_id"], succeeded=False)
    with pytest.raises(JobStateError, match="retry bound"):
        store.retry_lane(job["run_id"], "backend")
    store.close()


def test_non_resumable_lane_and_stale_worker_are_visible(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = _create(store, tmp_path / "repo", plan=_plan(resumable=False))
    claimed = store.claim_next(workspace=tmp_path / "repo", worker_pid=999999)
    assert claimed["status"] == "running"

    recovered = store.recover_stalled()
    assert recovered == [job["run_id"]]
    stalled = store.get_job(job["run_id"])
    assert stalled["status"] == "stalled"
    assert store.diagnostics()["counts"]["stalled"] == 1
    queued = _create(store, tmp_path / "repo")
    assert store.claim_next(workspace=tmp_path / "repo") is None
    with pytest.raises(JobStateError, match="non-resumable or non-idempotent lanes"):
        store.resume(job["run_id"])
    store.request_cancel(job["run_id"])
    assert store.claim_next(workspace=tmp_path / "repo")["run_id"] == queued["run_id"]
    store.close()


def test_resume_rejects_non_idempotent_lane_even_when_marked_resumable(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    plan = _plan()
    plan[0]["idempotent"] = False
    job = _create(store, tmp_path / "repo", plan=plan)
    store.claim_next(workspace=tmp_path / "repo", worker_pid=999999)
    store.recover_stalled()

    with pytest.raises(JobStateError, match="non-resumable or non-idempotent lanes"):
        store.resume(job["run_id"])
    store.close()


def test_stale_heartbeat_keeps_workspace_locked_while_worker_lives(tmp_path):
    database = tmp_path / "jobs.db"
    workspace = tmp_path / "repo"
    store = JobStore(database)
    active = _create(store, workspace)
    store.claim_next(workspace=workspace, worker_pid=os.getpid())
    queued = _create(store, workspace)
    with store.db:
        store.db.execute(
            "UPDATE jobs SET heartbeat_at = 0 WHERE run_id = ?", (active["run_id"],)
        )

    assert store.recover_stalled() == []
    assert store.get_job(active["run_id"])["status"] == "running"
    assert store.claim_next(workspace=workspace) is None
    assert active["run_id"] in inspect_job_database(database)["stalled_run_ids"]
    assert store.get_job(queued["run_id"])["status"] == "queued"
    store.close()


@pytest.mark.skipif(os.name != "posix", reason="delegated process groups require POSIX")
def test_stalled_recovery_kills_registered_orphan_process_group(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = _create(store, tmp_path / "repo")
    store.claim_next(workspace=tmp_path / "repo", worker_pid=999999)
    child_marker = tmp_path / "child-ready"
    child_result = tmp_path / "child-survived"
    child_code = (
        "import pathlib,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(child_marker)!r}).write_text('ready'); time.sleep(1); "
        f"pathlib.Path({str(child_result)!r}).write_text('bad')"
    )
    parent_code = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); time.sleep(30)"
    )
    process = subprocess.Popen([sys.executable, "-c", parent_code], start_new_session=True)
    store.register_process_group(job["run_id"], process.pid)
    try:
        for _ in range(100):
            if child_marker.exists():
                break
            time.sleep(0.02)
        assert child_marker.exists()
        assert store.recover_stalled() == [job["run_id"]]
        assert store.get_job(job["run_id"])["status"] == "stalled"
        assert store.db.execute(
            "SELECT 1 FROM job_process_groups WHERE run_id = ?", (job["run_id"],)
        ).fetchone() is None
        time.sleep(1.2)
        assert not child_result.exists()
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        process.wait(timeout=5)
        store.close()


def test_jobs_cli_exposes_bounded_control_actions():
    parser = build_parser()
    parsed = parser.parse_args(["jobs", "retry", "run-123", "--lane", "backend", "--json"])
    assert parsed.jobs_action == "retry"
    assert parsed.run_id == "run-123"
    assert parsed.lane == "backend"
    assert parsed.json is True
