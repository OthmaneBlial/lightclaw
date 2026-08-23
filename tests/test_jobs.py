from __future__ import annotations

import stat

import pytest

from core.jobs import JobConflictError, JobStateError, JobStore
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

    recovered = store.recover_stalled(now=float(claimed["heartbeat_at"]) + 121)
    assert recovered == [job["run_id"]]
    stalled = store.get_job(job["run_id"])
    assert stalled["status"] == "stalled"
    assert store.diagnostics()["counts"]["stalled"] == 1
    with pytest.raises(JobStateError, match="non-resumable lanes"):
        store.resume(job["run_id"])
    store.close()


def test_jobs_cli_exposes_bounded_control_actions():
    parser = build_parser()
    parsed = parser.parse_args(["jobs", "retry", "run-123", "--lane", "backend", "--json"])
    assert parsed.jobs_action == "retry"
    assert parsed.run_id == "run-123"
    assert parsed.lane == "backend"
    assert parsed.json is True
