from __future__ import annotations

import os
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

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


def _create(
    store: JobStore,
    workspace,
    *,
    priority=0,
    plan=None,
    status="queued",
    session_id="fixture",
    requester_user_id=None,
    resumable=True,
):
    return store.create_job(
        workspace=workspace,
        session_id=session_id,
        requester_user_id=requester_user_id,
        goal="bounded fixture goal",
        approved_scope="src only",
        risk_level="medium",
        capability_profile="workspace-write",
        plan=plan or _plan(),
        priority=priority,
        status=status,
        resumable=resumable,
    )


def test_jobs_persist_across_restart_with_private_database(tmp_path):
    path = tmp_path / "state" / "jobs.db"
    first = JobStore(path)
    created = _create(
        first, tmp_path / "repo", status="awaiting_approval", requester_user_id=7
    )
    first.approve(created["run_id"])
    first.close()

    second = JobStore(path)
    restored = second.get_job(created["run_id"])
    assert restored["status"] == "queued"
    assert restored["requester_user_id"] == 7
    assert [lane["label"] for lane in restored["lanes"]] == ["backend", "frontend"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    second.close()


def test_list_jobs_can_be_scoped_to_one_telegram_session(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    first = _create(store, tmp_path / "first", session_id="chat-one")
    _create(store, tmp_path / "second", session_id="chat-two")

    try:
        jobs = store.list_jobs(session_id="chat-one")
        assert [job["run_id"] for job in jobs] == [first["run_id"]]
        assert len(store.list_jobs()) == 2
    finally:
        store.close()


def test_list_jobs_offset_is_stable_when_creation_times_tie(tmp_path, monkeypatch):
    monkeypatch.setattr("core.jobs.time.time", lambda: 123.0)
    store = JobStore(tmp_path / "jobs.db")
    try:
        created = [
            _create(store, tmp_path / f"repo-{index}", session_id="chat-one")
            for index in range(3)
        ]
        jobs = store.list_jobs(session_id="chat-one")

        assert [job["run_id"] for job in jobs] == sorted(
            (job["run_id"] for job in created), reverse=True
        )
        assert store.list_jobs(session_id="chat-one", limit=1, offset=1) == jobs[1:2]
        assert store.list_jobs(session_id="chat-one", limit=1, offset=-1) == jobs[:1]
    finally:
        store.close()


def test_history_snapshot_prevents_new_jobs_from_shifting_pages(tmp_path, monkeypatch):
    monkeypatch.setattr("core.jobs.time.time", lambda: 123.0)
    store = JobStore(tmp_path / "jobs.db")
    try:
        for index in range(12):
            _create(
                store,
                tmp_path / f"repo-{index}",
                session_id="chat-one",
            )
        snapshot = store.history_snapshot("chat-one")
        original = store.list_jobs(session_id="chat-one")
        original_page = store.list_jobs(
            session_id="chat-one", limit=10, offset=0, snapshot_rowid=snapshot
        )
        _create(store, tmp_path / "other-chat", session_id="chat-two")
        assert store.history_snapshot("chat-one") == snapshot

        store.create_job(
            workspace=tmp_path / "new-repo",
            session_id="chat-one",
            goal="new run",
            approved_scope="src only",
            risk_level="medium",
            capability_profile="workspace-write",
            plan=_plan(),
            status="queued",
            run_id="zzzz-new",
        )

        stable_next_page = store.list_jobs(
            session_id="chat-one", limit=10, offset=10, snapshot_rowid=snapshot
        )
        shifted_next_page = store.list_jobs(session_id="chat-one", limit=10, offset=10)

        assert [job["run_id"] for job in original_page] == [
            job["run_id"] for job in original[:10]
        ]
        assert [job["run_id"] for job in stable_next_page] == [
            job["run_id"] for job in original[10:]
        ]
        assert shifted_next_page[0]["run_id"] == original[9]["run_id"]
    finally:
        store.close()


def test_job_history_query_uses_session_order_index(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    try:
        plan = store.db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM jobs WHERE session_id = ? AND rowid <= ? "
            "ORDER BY created_at DESC, run_id DESC LIMIT ? OFFSET ?",
            ("chat-one", store.history_snapshot("chat-one"), 11, 0),
        ).fetchall()

        assert any(
            "USING INDEX jobs_by_session_history" in str(row["detail"])
            for row in plan
        )
        assert all("TEMP B-TREE" not in str(row["detail"]) for row in plan)
    finally:
        store.close()


def test_job_read_uses_empty_values_for_recursively_nested_json(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "jobs.db")
    try:
        job = _create(store, tmp_path / "repo")

        def reject_nested_json(_content):
            raise RecursionError("maximum recursion depth exceeded")

        monkeypatch.setattr("core.jobs.json.loads", reject_nested_json)
        restored = store.get_job(job["run_id"])

        assert restored["plan"] == []
        assert all(lane["owned_paths"] == [] for lane in restored["lanes"])
        assert all(lane["depends_on"] == [] for lane in restored["lanes"])
    finally:
        store.close()


def test_job_store_keeps_wal_sidecars_private_in_shared_parent(tmp_path):
    parent = tmp_path / "shared"
    parent.mkdir()
    parent.chmod(0o755)
    database = parent / "jobs.db"
    legacy = sqlite3.connect(database)
    legacy.execute("PRAGMA journal_mode=WAL")
    legacy.execute("CREATE TABLE legacy (value TEXT)")
    legacy.execute("INSERT INTO legacy VALUES ('private state')")
    legacy.commit()
    os.chmod(database, 0o600)
    for suffix in ("-wal", "-shm"):
        os.chmod(database.with_name(database.name + suffix), 0o644)

    store = JobStore(database)
    _create(store, parent / "repo")

    for path in (database, database.with_name("jobs.db-wal"), database.with_name("jobs.db-shm")):
        assert path.exists()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    store.close()
    legacy.close()


def test_job_store_does_not_chmod_database_symlink_target(tmp_path, monkeypatch):
    database = tmp_path / "jobs.db"
    victim = tmp_path / "victim.db"
    victim.write_text("private", encoding="utf-8")
    os.chmod(victim, 0o644)
    fchmod = os.fchmod

    def swap_then_chmod(fd, mode):
        database.unlink()
        database.symlink_to(victim)
        return fchmod(fd, mode)

    monkeypatch.setattr("core.fs.os.fchmod", swap_then_chmod)
    with pytest.raises(OSError, match="changed during permission update"):
        JobStore(database)

    assert victim.read_text(encoding="utf-8") == "private"
    assert stat.S_IMODE(victim.stat().st_mode) == 0o644


def test_job_store_rejects_preexisting_database_symlink_without_changing_target(tmp_path):
    database = tmp_path / "jobs.db"
    victim = tmp_path / "victim.db"
    legacy = sqlite3.connect(victim)
    legacy.execute("CREATE TABLE private_data (value TEXT)")
    legacy.execute("INSERT INTO private_data VALUES ('keep this database intact')")
    legacy.commit()
    legacy.close()
    victim.chmod(0o644)
    original = victim.read_bytes()
    database.symlink_to(victim)

    with pytest.raises(OSError):
        JobStore(database)

    assert victim.read_bytes() == original
    assert stat.S_IMODE(victim.stat().st_mode) == 0o644


@pytest.mark.parametrize("suffix", ("-wal", "-shm"))
def test_job_store_rejects_symlinked_sidecar_without_chmod_target(tmp_path, suffix):
    database = tmp_path / "jobs.db"
    sqlite3.connect(database).close()
    database.chmod(0o600)
    victim = tmp_path / "victim.db"
    victim.write_text("private", encoding="utf-8")
    os.chmod(victim, 0o644)
    database.with_name(database.name + suffix).symlink_to(victim)

    with pytest.raises(OSError):
        JobStore(database)

    assert victim.read_text(encoding="utf-8") == "private"
    assert stat.S_IMODE(victim.stat().st_mode) == 0o644


def test_job_store_fails_closed_when_database_cannot_be_private(tmp_path, monkeypatch):
    def fail_chmod(*_args, **_kwargs):
        raise PermissionError("fixture permission failure")

    monkeypatch.setattr("core.fs.os.fchmod", fail_chmod)
    with pytest.raises(PermissionError, match="fixture permission failure"):
        JobStore(tmp_path / "jobs.db")


def test_job_store_closes_connection_when_schema_initialization_fails(tmp_path, monkeypatch):
    class TrackingConnection(sqlite3.Connection):
        closed = False

        def close(self):
            self.closed = True
            super().close()

    connect = sqlite3.connect
    connections = []

    def tracking_connect(*args, **kwargs):
        kwargs["factory"] = TrackingConnection
        connection = connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr("core.jobs.sqlite3.connect", tracking_connect)
    database = tmp_path / "corrupt.db"
    database.write_bytes(b"not a SQLite database")

    with pytest.raises(sqlite3.DatabaseError):
        JobStore(database)

    assert len(connections) == 1
    assert connections[0].closed


def test_concurrent_approval_transition_writes_one_event_across_connections(tmp_path):
    database = tmp_path / "jobs.db"
    stores = [JobStore(database) for _ in range(8)]
    job = _create(stores[0], tmp_path / "repo", status="awaiting_approval")
    barrier = threading.Barrier(len(stores))

    def approve(store: JobStore) -> bool:
        barrier.wait(timeout=5)
        try:
            store.approve(job["run_id"])
        except JobStateError:
            return False
        return True

    try:
        with ThreadPoolExecutor(max_workers=len(stores)) as pool:
            results = list(pool.map(approve, stores))
        event_count = stores[0].db.execute(
            "SELECT count(*) FROM job_events WHERE run_id = ? AND kind = 'approved'",
            (job["run_id"],),
        ).fetchone()[0]
        assert results.count(True) == 1
        assert event_count == 1
        assert stores[0].get_job(job["run_id"])["status"] == "queued"
    finally:
        for store in stores:
            store.close()


def test_concurrent_lane_retries_queue_one_attempt_across_connections(tmp_path):
    database = tmp_path / "jobs.db"
    stores = [JobStore(database) for _ in range(8)]
    job = _create(stores[0], tmp_path / "repo")
    stores[0].claim_next(workspace=tmp_path / "repo")
    stores[0].update_lane(job["run_id"], "backend", "running", increment_attempt=True)
    stores[0].update_lane(job["run_id"], "backend", "failed")
    stores[0].finish(job["run_id"], succeeded=False)
    barrier = threading.Barrier(len(stores))
    for store in stores:
        get_job = store.get_job

        def get_stale_job(run_id, get_job=get_job):
            value = get_job(run_id)
            if run_id == job["run_id"] and value["status"] == "failed":
                barrier.wait(timeout=5)
            return value

        store.get_job = get_stale_job

    def retry(store: JobStore) -> bool:
        try:
            store.retry_lane(job["run_id"], "backend")
        except JobStateError:
            return False
        return True

    try:
        with ThreadPoolExecutor(max_workers=len(stores)) as pool:
            results = list(pool.map(retry, stores))
        event_count = stores[0].db.execute(
            "SELECT count(*) FROM job_events WHERE run_id = ? AND kind = 'lane_retry_queued'",
            (job["run_id"],),
        ).fetchone()[0]
        assert results.count(True) == 1
        assert event_count == 1
        assert stores[0].get_job(job["run_id"])["retry_count"] == 1
    finally:
        for store in stores:
            store.close()


def test_late_lane_start_cannot_resurrect_canceled_lane(tmp_path):
    database = tmp_path / "jobs.db"
    store = JobStore(database)
    late_writer = JobStore(database)
    job = _create(store, tmp_path / "repo")
    store.claim_next(workspace=tmp_path / "repo")
    started = threading.Event()
    release = threading.Event()
    errors: list[JobStateError] = []

    def start_late_lane() -> None:
        started.set()
        if not release.wait(timeout=5):
            return
        try:
            late_writer.update_lane(
                job["run_id"], "backend", "running", increment_attempt=True
            )
        except JobStateError as exc:
            errors.append(exc)

    thread = threading.Thread(target=start_late_lane)
    thread.start()
    try:
        assert started.wait(timeout=5)
        store.request_cancel(job["run_id"])
        store.update_lane(job["run_id"], "backend", "canceled")
        store.mark_canceled(job["run_id"])
        release.set()
        thread.join(timeout=5)

        assert not thread.is_alive()
        assert errors
        assert store.get_job(job["run_id"])["lanes"][0]["status"] == "canceled"
    finally:
        release.set()
        thread.join(timeout=5)
        late_writer.close()
        store.close()


@pytest.mark.parametrize("stop", ["cancel_requested", "canceled"])
def test_lane_start_refuses_a_job_that_has_been_stopped(tmp_path, stop):
    store = JobStore(tmp_path / "jobs.db")
    workspace = tmp_path / "repo"
    job = _create(store, workspace)
    store.claim_next(workspace=workspace)
    store.request_cancel(job["run_id"])
    if stop == "canceled":
        store.mark_canceled(job["run_id"])
    before = store.get_job(job["run_id"])
    try:
        with pytest.raises(JobStateError):
            store.update_lane(job["run_id"], "backend", "running", increment_attempt=True)
        assert store.get_job(job["run_id"]) == before
    finally:
        store.close()


def test_lane_start_enforces_attempt_bound_and_refuses_duplicate_start(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    workspace = tmp_path / "repo"
    plan = _plan()
    plan[0]["max_attempts"] = 1
    job = _create(store, workspace, plan=plan)
    store.claim_next(workspace=workspace)
    store.update_lane(job["run_id"], "backend", "running", increment_attempt=True)
    try:
        with pytest.raises(JobStateError):
            store.update_lane(job["run_id"], "backend", "running", increment_attempt=True)
        store.update_lane(job["run_id"], "backend", "queued")
        with pytest.raises(JobStateError):
            store.update_lane(job["run_id"], "backend", "running", increment_attempt=True)
        lane = store.get_job(job["run_id"])["lanes"][0]
        assert lane["status"] == "queued"
        assert lane["attempt"] == 1
    finally:
        store.close()


def test_concurrent_stall_recovery_writes_one_event_across_connections(tmp_path):
    database = tmp_path / "jobs.db"
    stores = [JobStore(database) for _ in range(2)]
    job = _create(stores[0], tmp_path / "repo")
    stores[0].claim_next(workspace=tmp_path / "repo", worker_pid=999999)
    barrier = threading.Barrier(len(stores))
    for store in stores:
        stop_process_groups = store._stop_process_groups

        def wait_for_recovery(run_id, stop=stop_process_groups, **kwargs):
            barrier.wait(timeout=5)
            return stop(run_id, **kwargs)

        store._stop_process_groups = wait_for_recovery

    try:
        with ThreadPoolExecutor(max_workers=len(stores)) as pool:
            results = list(pool.map(lambda store: store.recover_stalled(), stores))
        event_count = stores[0].db.execute(
            "SELECT count(*) FROM job_events WHERE run_id = ? AND kind = 'stalled'",
            (job["run_id"],),
        ).fetchone()[0]
        assert sum(len(result) for result in results) == 1
        assert event_count == 1
        assert stores[0].get_job(job["run_id"])["status"] == "stalled"
    finally:
        for store in stores:
            store.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="requires Darwin proc_pidinfo")
def test_macos_process_start_tokens_distinguish_processes_with_microseconds():
    processes = []
    try:
        for _ in range(2):
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(5)"],
                    start_new_session=True,
                )
            )
        tokens = [JobStore._process_start_token(process.pid) for process in processes]
        assert len(set(tokens)) == len(processes)
        for token in tokens:
            seconds, microseconds = token.split(":")
            assert int(seconds) > 0
            assert len(microseconds) == 6
            assert 0 <= int(microseconds) < 1_000_000
    finally:
        for process in processes:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)


@pytest.mark.skipif(os.name != "posix", reason="delegated process groups require POSIX")
@pytest.mark.parametrize("operation", ["recovery", "resume", "cancel", "retry"])
def test_stale_job_control_cannot_stop_a_resumed_run(tmp_path, monkeypatch, operation):
    database = tmp_path / "jobs.db"
    stale = JobStore(database)
    current = JobStore(database)
    job = _create(stale, tmp_path / "repo")
    stale.claim_next(workspace=tmp_path / "repo", worker_pid=999999)
    stale.update_lane(job["run_id"], "backend", "running", increment_attempt=True)
    stale.update_lane(job["run_id"], "backend", "failed")
    if operation != "recovery":
        assert current.recover_stalled() == [job["run_id"]]
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )

    def restart_run():
        if operation == "recovery":
            assert current.recover_stalled() == [job["run_id"]]
        current.retry_lane(job["run_id"], "backend")
        current.claim_next(workspace=tmp_path / "repo", worker_pid=os.getpid())
        current.register_process_group(job["run_id"], process.pid)

    def resume_before_probe_completes(_pid, _token):
        restart_run()
        return False

    get_job = stale.get_job

    def read_old_job(run_id):
        snapshot = get_job(run_id)
        monkeypatch.setattr(stale, "get_job", get_job)
        restart_run()
        return snapshot

    try:
        if operation == "recovery":
            monkeypatch.setattr(stale, "_worker_process_alive", resume_before_probe_completes)
            assert stale.recover_stalled() == []
        else:
            monkeypatch.setattr(stale, "get_job", read_old_job)
            with pytest.raises(JobStateError):
                if operation == "retry":
                    stale.retry_lane(job["run_id"], "backend")
                elif operation == "resume":
                    stale.resume(job["run_id"])
                else:
                    stale.request_cancel(job["run_id"])
        assert current.get_job(job["run_id"])["status"] == "running"
        assert process.poll() is None
        assert current.db.execute(
            "SELECT pgid FROM job_process_groups WHERE run_id = ?", (job["run_id"],)
        ).fetchone()["pgid"] == process.pid
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        stale.close()
        current.close()


def test_priority_queue_and_one_active_writer_per_workspace(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    workspace = tmp_path / "repo"
    low = _create(store, workspace, priority=1)
    high = _create(store, workspace, priority=50)

    assert store.claim_next(workspace=workspace, run_id=low["run_id"]) is None
    assert all(job["status"] == "queued" for job in store.list_jobs())
    claimed = store.claim_next(workspace=workspace, worker_pid=12345, run_id=high["run_id"])
    assert claimed["run_id"] == high["run_id"]
    assert store.claim_next(workspace=workspace, worker_pid=12346) is None
    store.finish(high["run_id"], succeeded=True)
    assert store.claim_next(workspace=workspace, worker_pid=12346)["run_id"] == low["run_id"]
    store.close()


@pytest.mark.parametrize("blocking_status", ["running", "cancel_requested", "stalled"])
def test_global_queue_skips_blocked_workspaces(tmp_path, blocking_status):
    store = JobStore(tmp_path / "jobs.db")
    blocked_workspace = tmp_path / "busy"
    active = _create(store, blocked_workspace)
    store.claim_next(workspace=blocked_workspace, worker_pid=999999)
    if blocking_status == "cancel_requested":
        store.request_cancel(active["run_id"])
    elif blocking_status == "stalled":
        assert store.recover_stalled() == [active["run_id"]]
    blocked = _create(store, blocked_workspace, priority=100)
    ready_workspace = tmp_path / "ready"
    low = _create(store, ready_workspace, priority=1)
    high = _create(store, ready_workspace, priority=50)
    try:
        claimed = store.claim_next()
        assert claimed is not None
        assert claimed["run_id"] == high["run_id"]
        assert store.claim_next() is None
        assert store.get_job(active["run_id"])["status"] == blocking_status
        assert store.get_job(blocked["run_id"])["status"] == "queued"
        store.finish(high["run_id"], succeeded=True)
        assert store.claim_next()["run_id"] == low["run_id"]
    finally:
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


def test_cancel_queued_job_cancels_unstarted_lanes_atomically(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = _create(store, tmp_path / "repo")

    canceled = store.request_cancel(job["run_id"])

    assert canceled["status"] == "canceled"
    assert all(lane["status"] == "canceled" for lane in canceled["lanes"])
    assert store.claim_next(workspace=tmp_path / "repo") is None
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


@pytest.mark.parametrize("disabled", ["job", "lane", "idempotence"])
def test_lane_retry_respects_explicit_recovery_restrictions(tmp_path, disabled):
    store = JobStore(tmp_path / "jobs.db")
    workspace = tmp_path / "repo"
    plan = _plan()
    if disabled == "lane":
        plan[0]["resumable"] = False
    elif disabled == "idempotence":
        plan[0]["idempotent"] = False
    job = _create(store, workspace, plan=plan, resumable=disabled != "job")
    store.claim_next(workspace=workspace)
    store.update_lane(job["run_id"], "backend", "running", increment_attempt=True)
    store.update_lane(job["run_id"], "backend", "failed")
    failed = store.finish(job["run_id"], succeeded=False)
    try:
        reason = "non-idempotent" if disabled == "idempotence" else "non-resumable"
        with pytest.raises(JobStateError, match=reason):
            store.retry_lane(job["run_id"], "backend")
        assert store.get_job(job["run_id"]) == failed
    finally:
        store.close()


def test_job_diagnostics_can_be_scoped_to_one_telegram_session(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    own_workspace = tmp_path / "own"
    other_workspace = tmp_path / "other"
    own_job = _create(store, own_workspace, session_id="chat-own")
    other_job = _create(store, other_workspace, session_id="chat-other")
    store.claim_next(workspace=own_workspace, worker_pid=999999)
    store.claim_next(workspace=other_workspace, worker_pid=999998)

    report = store.diagnostics(session_id="chat-own")

    assert report["counts"] == {"running": 1}
    assert [job["run_id"] for job in report["active"]] == [own_job["run_id"]]
    assert other_job["run_id"] not in {job["run_id"] for job in report["active"]}
    store.close()


def test_resume_requeues_interrupted_lane_and_counts_new_attempt(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = _create(store, tmp_path / "repo")
    store.claim_next(workspace=tmp_path / "repo", worker_pid=999999)
    store.update_lane(job["run_id"], "backend", "running", increment_attempt=True)

    assert store.recover_stalled() == [job["run_id"]]
    assert store.resume(job["run_id"])["lanes"][0]["status"] == "queued"
    store.claim_next(workspace=tmp_path / "repo")
    resumed = store.update_lane(
        job["run_id"], "backend", "running", increment_attempt=True
    )

    assert resumed["lanes"][0]["attempt"] == 2
    store.close()


def test_resumed_failed_job_clears_previous_completion_timestamp(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    workspace = tmp_path / "repo"
    job = _create(store, workspace)
    claimed = store.claim_next(workspace=workspace)
    failed = store.finish(job["run_id"], succeeded=False)
    assert failed["finished_at"] is not None
    try:
        resumed = store.resume(job["run_id"])
        assert resumed["status"] == "queued"
        assert resumed["finished_at"] is None
        restarted = store.claim_next(workspace=workspace)
        assert restarted["finished_at"] is None
        assert restarted["started_at"] == claimed["started_at"]
        assert store.finish(job["run_id"], succeeded=True)["finished_at"] is not None
    finally:
        store.close()


def test_resume_refuses_to_exceed_interrupted_lane_attempt_bound(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    plan = _plan()
    plan[0]["max_attempts"] = 1
    job = _create(store, tmp_path / "repo", plan=plan)
    store.claim_next(workspace=tmp_path / "repo", worker_pid=999999)
    store.update_lane(job["run_id"], "backend", "running", increment_attempt=True)
    store.recover_stalled()

    with pytest.raises(JobStateError, match="lane retry bound"):
        store.resume(job["run_id"])
    assert store.get_job(job["run_id"])["status"] == "stalled"
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


def test_recovery_detects_reused_worker_pid(tmp_path):
    database = tmp_path / "jobs.db"
    store = JobStore(database)
    job = _create(store, tmp_path / "repo")
    store.claim_next(workspace=tmp_path / "repo", worker_pid=os.getpid())
    with store.db:
        store.db.execute(
            "UPDATE jobs SET worker_start_token = 'different-process' WHERE run_id = ?",
            (job["run_id"],),
        )

    assert job["run_id"] in inspect_job_database(database)["stalled_run_ids"]
    assert store.recover_stalled() == [job["run_id"]]
    assert "replaced" in store.get_job(job["run_id"])["last_error"]
    store.close()


@pytest.mark.parametrize("probe_error", [PermissionError("probe denied"), OSError("probe failed")])
def test_failed_worker_probe_keeps_live_workspace_locked(tmp_path, monkeypatch, probe_error):
    database = tmp_path / "jobs.db"
    workspace = tmp_path / "repo"
    store = JobStore(database)
    active = _create(store, workspace)
    store.claim_next(workspace=workspace, worker_pid=os.getpid())
    queued = _create(store, workspace)

    def refuse_probe(pid, sig):
        assert pid == os.getpid() and sig == 0
        raise probe_error

    monkeypatch.setattr("core.jobs.os.kill", refuse_probe)
    try:
        assert store.recover_stalled() == []
        assert store.get_job(active["run_id"])["status"] == "running"
        assert store.claim_next(workspace=workspace) is None
        assert store.get_job(queued["run_id"])["status"] == "queued"
        assert inspect_job_database(database)["stalled_run_ids"] == []
    finally:
        store.close()


def test_legacy_job_database_migrates_worker_identity_and_requester_columns(tmp_path):
    database = tmp_path / "jobs.db"
    now = time.time()
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE jobs (
            run_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            workspace TEXT NOT NULL,
            status TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            heartbeat_at REAL,
            worker_pid INTEGER
        );
        """
    )
    connection.execute(
        "INSERT INTO jobs(run_id, schema_version, workspace, status, created_at, updated_at, heartbeat_at, worker_pid) "
        "VALUES ('legacy-run', 1, ?, 'running', ?, ?, ?, ?)",
        (str(tmp_path / "repo"), now, now, now, os.getpid()),
    )
    connection.commit()
    connection.close()

    # Read-only diagnostics still support databases before the migration runs.
    assert inspect_job_database(database)["stalled_run_ids"] == []
    store = JobStore(database)
    columns = {row["name"] for row in store.db.execute("PRAGMA table_info(jobs)")}
    migrated = store.db.execute(
        "SELECT schema_version, worker_start_token, requester_user_id "
        "FROM jobs WHERE run_id = 'legacy-run'"
    ).fetchone()
    assert "worker_start_token" in columns
    assert "requester_user_id" in columns
    assert migrated["schema_version"] == 3
    assert migrated["worker_start_token"] == ""
    assert migrated["requester_user_id"] is None
    assert store.recover_stalled() == []
    store.close()


def test_job_diagnostics_refuse_database_symlink(tmp_path):
    database = tmp_path / "jobs.db"
    victim = tmp_path / "victim.db"
    connection = sqlite3.connect(victim)
    connection.execute("CREATE TABLE jobs (status TEXT)")
    connection.execute("INSERT INTO jobs VALUES ('running')")
    connection.commit()
    connection.close()
    database.symlink_to(victim)

    report = inspect_job_database(database)

    assert report["exists"] is True
    assert report["database"] == database.as_posix()
    assert "symlink" in report["error"]
    assert report["counts"] == {}


def test_job_diagnostics_handles_uri_reserved_path_characters(tmp_path):
    database = tmp_path / "jobs?#.db"
    store = JobStore(database)
    _create(store, tmp_path / "repo")
    store.close()

    report = inspect_job_database(database)

    assert "error" not in report
    assert report["counts"]["queued"] == 1


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


@pytest.mark.skipif(os.name != "posix", reason="delegated process groups require POSIX")
def test_stalled_recovery_kills_group_after_registered_leader_exits(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = _create(store, tmp_path / "repo")
    store.claim_next(workspace=tmp_path / "repo", worker_pid=999999)
    child_marker = tmp_path / "child-ready"
    child_result = tmp_path / "child-survived"
    child_code = (
        "import pathlib,time; "
        f"pathlib.Path({str(child_marker)!r}).write_text('ready'); time.sleep(1.5); "
        f"pathlib.Path({str(child_result)!r}).write_text('bad')"
    )
    parent_code = (
        "import pathlib,subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(.3)"
    )
    process = subprocess.Popen([sys.executable, "-c", parent_code], start_new_session=True)
    store.register_process_group(job["run_id"], process.pid)
    try:
        for _ in range(100):
            if child_marker.exists():
                break
            time.sleep(0.02)
        assert child_marker.exists()
        process.wait(timeout=5)
        assert store._process_group_exists(process.pid)

        assert store.recover_stalled() == [job["run_id"]]
        assert store.db.execute(
            "SELECT 1 FROM job_process_groups WHERE run_id = ?", (job["run_id"],)
        ).fetchone() is None
        time.sleep(1.7)
        assert not child_result.exists()
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        if process.poll() is None:
            process.wait(timeout=5)
        store.close()


def test_jobs_cli_exposes_bounded_control_actions():
    parser = build_parser()
    parsed = parser.parse_args(["jobs", "retry", "run-123", "--lane", "backend", "--json"])
    assert parsed.jobs_action == "retry"
    assert parsed.run_id == "run-123"
    assert parsed.lane == "backend"
    assert parsed.json is True
