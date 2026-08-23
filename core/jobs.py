"""Durable SQLite job control for approved LightClaw workspace runs."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path, PurePosixPath

JOB_SCHEMA_VERSION = 1
TERMINAL_STATUSES = frozenset({"canceled", "failed", "succeeded", "accepted", "rejected"})
RESUMABLE_STATUSES = frozenset({"paused", "stalled", "failed"})


class JobStateError(ValueError):
    """Raised when a requested durable job transition is unsafe."""


class JobConflictError(JobStateError):
    """Raised when workspace or lane ownership conflicts."""


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _decode(value: str | None, fallback):
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def _normalize_owned_path(raw: object) -> str:
    text = str(raw or "").strip().replace("\\", "/")
    candidate = PurePosixPath(text)
    if not text or candidate.is_absolute() or ".." in candidate.parts:
        raise JobConflictError(f"owned path must be safe and relative: {text or '(empty)'}")
    normalized = candidate.as_posix().strip("/")
    if not normalized or normalized == ".":
        raise JobConflictError("owned path must identify a bounded workspace path")
    return normalized


def paths_overlap(first: str, second: str) -> bool:
    """Return True when two normalized paths own the same tree."""
    left = _normalize_owned_path(first)
    right = _normalize_owned_path(second)
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def detect_parallel_path_conflicts(plan: list[dict[str, object]]) -> list[dict[str, str]]:
    """Detect overlapping paths only for lanes that may execute concurrently."""
    labels = [str(item.get("label") or "").strip() for item in plan]
    if not all(labels) or len(labels) != len(set(labels)):
        raise JobConflictError("every lane must have a unique non-empty label")
    dependencies: dict[str, set[str]] = {}
    owned: dict[str, list[str]] = {}
    known = set(labels)
    for item, label in zip(plan, labels, strict=True):
        raw_dependencies = item.get("depends_on")
        deps = {
            str(value).strip()
            for value in raw_dependencies
            if str(value).strip()
        } if isinstance(raw_dependencies, list) else set()
        if label in deps or not deps.issubset(known):
            raise JobConflictError(f"lane {label} has an invalid dependency")
        dependencies[label] = deps
        raw_paths = item.get("owned_paths")
        owned[label] = (
            [_normalize_owned_path(value) for value in raw_paths]
            if isinstance(raw_paths, list)
            else []
        )

    def ancestors(label: str, trail: set[str] | None = None) -> set[str]:
        trail = set(trail or ())
        if label in trail:
            raise JobConflictError("lane dependency graph contains a cycle")
        trail.add(label)
        result: set[str] = set()
        for dependency in dependencies[label]:
            result.add(dependency)
            result.update(ancestors(dependency, trail))
        return result

    ancestry = {label: ancestors(label) for label in labels}
    conflicts: list[dict[str, str]] = []
    for index, left in enumerate(labels):
        for right in labels[index + 1 :]:
            if left in ancestry[right] or right in ancestry[left]:
                continue
            for left_path in owned[left]:
                for right_path in owned[right]:
                    if paths_overlap(left_path, right_path):
                        conflicts.append(
                            {
                                "left_lane": left,
                                "left_path": left_path,
                                "right_lane": right,
                                "right_path": right_path,
                            }
                        )
    return conflicts


class JobStore:
    """Persist jobs, lanes, leases, retries, and bounded event evidence."""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    run_id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    workspace TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    approved_scope TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    capability_profile TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 0,
                    resumable INTEGER NOT NULL DEFAULT 1,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL DEFAULT 1,
                    worker_pid INTEGER,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    heartbeat_at REAL,
                    last_error TEXT NOT NULL DEFAULT ''
                );
                DROP INDEX IF EXISTS one_active_writer_per_workspace;
                CREATE UNIQUE INDEX one_active_writer_per_workspace
                    ON jobs(workspace) WHERE status IN ('running', 'cancel_requested');
                CREATE INDEX IF NOT EXISTS queued_jobs
                    ON jobs(status, priority DESC, created_at ASC);
                CREATE TABLE IF NOT EXISTS lanes (
                    run_id TEXT NOT NULL REFERENCES jobs(run_id) ON DELETE CASCADE,
                    label TEXT NOT NULL,
                    status TEXT NOT NULL,
                    idempotent INTEGER NOT NULL,
                    resumable INTEGER NOT NULL,
                    owned_paths_json TEXT NOT NULL,
                    depends_on_json TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 2,
                    last_error TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (run_id, label)
                );
                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES jobs(run_id) ON DELETE CASCADE,
                    recorded_at REAL NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                """
            )
            self.db.commit()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def _event(self, run_id: str, kind: str, payload: dict[str, object] | None = None) -> None:
        self.db.execute(
            "INSERT INTO job_events(run_id, recorded_at, kind, payload_json) VALUES (?, ?, ?, ?)",
            (run_id, time.time(), kind, _json(payload or {})),
        )

    def create_job(
        self,
        *,
        workspace: str | Path,
        session_id: str,
        goal: str,
        approved_scope: str,
        risk_level: str,
        capability_profile: str,
        plan: list[dict[str, object]],
        priority: int = 0,
        max_retries: int = 1,
        resumable: bool = True,
        status: str = "awaiting_approval",
        run_id: str | None = None,
    ) -> dict[str, object]:
        conflicts = detect_parallel_path_conflicts(plan)
        if conflicts:
            first = conflicts[0]
            raise JobConflictError(
                "parallel lanes overlap: "
                f"{first['left_lane']}:{first['left_path']} and "
                f"{first['right_lane']}:{first['right_path']}"
            )
        if status not in {"awaiting_approval", "queued"}:
            raise JobStateError("new jobs must await approval or be explicitly queued")
        identifier = str(run_id or f"run-{uuid.uuid4().hex[:16]}")
        workspace_path = Path(workspace).expanduser().resolve().as_posix()
        now = time.time()
        with self._lock, self.db:
            self.db.execute(
                """
                INSERT INTO jobs(
                    run_id, schema_version, workspace, session_id, goal, approved_scope,
                    risk_level, capability_profile, plan_json, status, priority, resumable,
                    max_retries, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    JOB_SCHEMA_VERSION,
                    workspace_path,
                    str(session_id),
                    str(goal),
                    str(approved_scope),
                    str(risk_level),
                    str(capability_profile),
                    _json(plan),
                    status,
                    int(priority),
                    int(bool(resumable)),
                    max(0, int(max_retries)),
                    now,
                    now,
                ),
            )
            for item in plan:
                label = str(item.get("label") or "").strip()
                raw_paths = item.get("owned_paths")
                paths = (
                    [_normalize_owned_path(value) for value in raw_paths]
                    if isinstance(raw_paths, list)
                    else []
                )
                deps = item.get("depends_on") if isinstance(item.get("depends_on"), list) else []
                idempotent = bool(item.get("idempotent", False))
                lane_resumable = bool(item.get("resumable", idempotent))
                self.db.execute(
                    """
                    INSERT INTO lanes(
                        run_id, label, status, idempotent, resumable, owned_paths_json,
                        depends_on_json, max_attempts
                    ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        label,
                        int(idempotent),
                        int(lane_resumable),
                        _json(paths),
                        _json(deps),
                        max(1, int(item.get("max_attempts", max_retries + 1))),
                    ),
                )
            self._event(identifier, "created", {"status": status, "priority": int(priority)})
        return self.get_job(identifier)

    def _row_to_job(self, row: sqlite3.Row) -> dict[str, object]:
        lanes = self.db.execute(
            "SELECT * FROM lanes WHERE run_id = ? ORDER BY rowid", (row["run_id"],)
        ).fetchall()
        return {
            "run_id": row["run_id"],
            "schema_version": row["schema_version"],
            "workspace": row["workspace"],
            "session_id": row["session_id"],
            "goal": row["goal"],
            "approved_scope": row["approved_scope"],
            "risk_level": row["risk_level"],
            "capability_profile": row["capability_profile"],
            "plan": _decode(row["plan_json"], []),
            "status": row["status"],
            "priority": row["priority"],
            "resumable": bool(row["resumable"]),
            "retry_count": row["retry_count"],
            "max_retries": row["max_retries"],
            "worker_pid": row["worker_pid"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "heartbeat_at": row["heartbeat_at"],
            "last_error": row["last_error"],
            "lanes": [
                {
                    "label": lane["label"],
                    "status": lane["status"],
                    "idempotent": bool(lane["idempotent"]),
                    "resumable": bool(lane["resumable"]),
                    "owned_paths": _decode(lane["owned_paths_json"], []),
                    "depends_on": _decode(lane["depends_on_json"], []),
                    "attempt": lane["attempt"],
                    "max_attempts": lane["max_attempts"],
                    "last_error": lane["last_error"],
                }
                for lane in lanes
            ],
        }

    def get_job(self, run_id: str) -> dict[str, object]:
        with self._lock:
            row = self.db.execute("SELECT * FROM jobs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise JobStateError(f"unknown run: {run_id}")
            return self._row_to_job(row)

    def list_jobs(
        self,
        *,
        status: str | None = None,
        workspace: str | Path | None = None,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        clauses: list[str] = []
        params: list[object] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if workspace is not None:
            clauses.append("workspace = ?")
            params.append(Path(workspace).expanduser().resolve().as_posix())
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        query = f"SELECT * FROM jobs{where} ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(500, int(limit))))
        with self._lock:
            rows = self.db.execute(query, params).fetchall()
            return [self._row_to_job(row) for row in rows]

    def approve(self, run_id: str) -> dict[str, object]:
        return self._transition(run_id, {"awaiting_approval"}, "queued", "approved")

    def _transition(
        self,
        run_id: str,
        allowed: set[str],
        target: str,
        event: str,
        *,
        error: str = "",
    ) -> dict[str, object]:
        now = time.time()
        with self._lock, self.db:
            row = self.db.execute("SELECT status FROM jobs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise JobStateError(f"unknown run: {run_id}")
            if row["status"] not in allowed:
                raise JobStateError(f"cannot transition {row['status']} to {target}")
            finished = now if target in TERMINAL_STATUSES else None
            self.db.execute(
                "UPDATE jobs SET status = ?, updated_at = ?, finished_at = COALESCE(?, finished_at), last_error = ? WHERE run_id = ?",
                (target, now, finished, str(error), run_id),
            )
            self._event(run_id, event, {"from": row["status"], "to": target})
        return self.get_job(run_id)

    def claim_next(self, *, workspace: str | Path | None = None, worker_pid: int | None = None) -> dict[str, object] | None:
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                params: list[object] = []
                clause = "status = 'queued'"
                if workspace is not None:
                    clause += " AND workspace = ?"
                    params.append(Path(workspace).expanduser().resolve().as_posix())
                row = self.db.execute(
                    f"SELECT run_id, workspace FROM jobs WHERE {clause} ORDER BY priority DESC, created_at ASC LIMIT 1",
                    params,
                ).fetchone()
                if row is None:
                    self.db.commit()
                    return None
                active = self.db.execute(
                    "SELECT run_id FROM jobs WHERE workspace = ? AND status IN ('running', 'cancel_requested')",
                    (row["workspace"],),
                ).fetchone()
                if active is not None:
                    self.db.commit()
                    return None
                now = time.time()
                self.db.execute(
                    "UPDATE jobs SET status = 'running', worker_pid = ?, started_at = COALESCE(started_at, ?), heartbeat_at = ?, updated_at = ? WHERE run_id = ? AND status = 'queued'",
                    (worker_pid or os.getpid(), now, now, now, row["run_id"]),
                )
                self._event(row["run_id"], "claimed", {"worker_pid": worker_pid or os.getpid()})
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        return self.get_job(row["run_id"])

    def heartbeat(self, run_id: str, *, worker_pid: int | None = None) -> dict[str, object]:
        now = time.time()
        with self._lock, self.db:
            updated = self.db.execute(
                "UPDATE jobs SET heartbeat_at = ?, updated_at = ?, worker_pid = COALESCE(?, worker_pid) WHERE run_id = ? AND status IN ('running', 'cancel_requested')",
                (now, now, worker_pid, run_id),
            )
            if updated.rowcount != 1:
                raise JobStateError("heartbeat requires a running job")
        return self.get_job(run_id)

    def request_cancel(self, run_id: str) -> dict[str, object]:
        job = self.get_job(run_id)
        if job["status"] in {"awaiting_approval", "queued", "paused", "stalled"}:
            return self._transition(run_id, {str(job["status"])}, "canceled", "canceled")
        if job["status"] == "running":
            return self._transition(run_id, {"running"}, "cancel_requested", "cancel_requested")
        raise JobStateError(f"cannot cancel job in state {job['status']}")

    def mark_canceled(self, run_id: str) -> dict[str, object]:
        return self._transition(run_id, {"running", "cancel_requested"}, "canceled", "canceled")

    def pause(self, run_id: str) -> dict[str, object]:
        return self._transition(run_id, {"running", "cancel_requested"}, "paused", "paused")

    def resume(self, run_id: str) -> dict[str, object]:
        job = self.get_job(run_id)
        if str(job["status"]) not in RESUMABLE_STATUSES:
            raise JobStateError(f"cannot resume job in state {job['status']}")
        if not job["resumable"]:
            raise JobStateError("job is explicitly non-resumable")
        unsafe = [lane["label"] for lane in job["lanes"] if not lane["resumable"]]
        if unsafe:
            raise JobStateError("non-resumable lanes block resume: " + ", ".join(unsafe))
        return self._transition(run_id, {str(job["status"])}, "queued", "resumed")

    def update_lane(
        self,
        run_id: str,
        label: str,
        status: str,
        *,
        error: str = "",
        increment_attempt: bool = False,
    ) -> dict[str, object]:
        allowed = {"queued", "running", "succeeded", "failed", "skipped", "canceled"}
        if status not in allowed:
            raise JobStateError(f"invalid lane state: {status}")
        with self._lock, self.db:
            updated = self.db.execute(
                """
                UPDATE lanes SET status = ?, last_error = ?,
                    attempt = attempt + ?
                WHERE run_id = ? AND label = ?
                """,
                (status, str(error), int(increment_attempt), run_id, label),
            )
            if updated.rowcount != 1:
                raise JobStateError(f"unknown lane: {label}")
            self._event(run_id, "lane_updated", {"label": label, "status": status})
        return self.get_job(run_id)

    def retry_lane(self, run_id: str, label: str) -> dict[str, object]:
        job = self.get_job(run_id)
        if job["status"] not in {"failed", "paused", "stalled"}:
            raise JobStateError(f"cannot retry a lane while job is {job['status']}")
        lane = next((item for item in job["lanes"] if item["label"] == label), None)
        if lane is None:
            raise JobStateError(f"unknown lane: {label}")
        if lane["status"] != "failed":
            raise JobStateError("only a failed lane may be retried")
        if not lane["idempotent"]:
            raise JobStateError("lane is explicitly non-idempotent")
        if int(lane["attempt"]) >= int(lane["max_attempts"]):
            raise JobStateError("lane retry bound has been reached")
        with self._lock, self.db:
            self.db.execute(
                "UPDATE lanes SET status = 'queued', last_error = '' WHERE run_id = ? AND label = ?",
                (run_id, label),
            )
            self.db.execute(
                "UPDATE jobs SET status = 'queued', retry_count = retry_count + 1, updated_at = ?, finished_at = NULL WHERE run_id = ? AND status IN ('failed', 'paused', 'stalled')",
                (time.time(), run_id),
            )
            self._event(run_id, "lane_retry_queued", {"label": label})
        return self.get_job(run_id)

    def finish(self, run_id: str, *, succeeded: bool, error: str = "") -> dict[str, object]:
        target = "succeeded" if succeeded else "failed"
        return self._transition(
            run_id,
            {"running", "cancel_requested"},
            target,
            target,
            error=error,
        )

    @staticmethod
    def _pid_alive(pid: int | None) -> bool:
        if not pid or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def recover_stalled(self, *, stall_after_seconds: int = 120, now: float | None = None) -> list[str]:
        current = float(now if now is not None else time.time())
        cutoff = current - max(1, int(stall_after_seconds))
        recovered: list[str] = []
        with self._lock, self.db:
            rows = self.db.execute(
                "SELECT run_id, worker_pid, heartbeat_at FROM jobs WHERE status IN ('running', 'cancel_requested')"
            ).fetchall()
            for row in rows:
                heartbeat = float(row["heartbeat_at"] or 0.0)
                if heartbeat >= cutoff and self._pid_alive(row["worker_pid"]):
                    continue
                reason = "worker process is absent" if not self._pid_alive(row["worker_pid"]) else "heartbeat is stale"
                self.db.execute(
                    "UPDATE jobs SET status = 'stalled', updated_at = ?, last_error = ? WHERE run_id = ?",
                    (current, reason, row["run_id"]),
                )
                self._event(row["run_id"], "stalled", {"reason": reason})
                recovered.append(str(row["run_id"]))
        return recovered

    def diagnostics(self) -> dict[str, object]:
        with self._lock:
            rows = self.db.execute(
                "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status ORDER BY status"
            ).fetchall()
            counts = {str(row["status"]): int(row["count"]) for row in rows}
            active = self.db.execute(
                "SELECT run_id, workspace, heartbeat_at, worker_pid FROM jobs WHERE status IN ('running', 'cancel_requested', 'stalled') ORDER BY updated_at DESC LIMIT 20"
            ).fetchall()
        return {
            "schema_version": JOB_SCHEMA_VERSION,
            "database": self.path.as_posix(),
            "counts": counts,
            "active": [dict(row) for row in active],
        }


def inspect_job_database(db_path: str | Path, *, stall_after_seconds: int = 120) -> dict[str, object]:
    """Inspect durable state without creating or mutating a database."""
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        return {
            "schema_version": JOB_SCHEMA_VERSION,
            "database": path.as_posix(),
            "exists": False,
            "counts": {},
            "active": [],
            "stalled_run_ids": [],
        }
    try:
        db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=3)
        db.row_factory = sqlite3.Row
        counts = {
            str(row["status"]): int(row["count"])
            for row in db.execute(
                "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status ORDER BY status"
            ).fetchall()
        }
        active_rows = db.execute(
            "SELECT run_id, workspace, status, heartbeat_at, worker_pid FROM jobs WHERE status IN ('running', 'cancel_requested', 'stalled') ORDER BY updated_at DESC LIMIT 20"
        ).fetchall()
        db.close()
    except (OSError, sqlite3.Error) as exc:
        return {
            "schema_version": JOB_SCHEMA_VERSION,
            "database": path.as_posix(),
            "exists": True,
            "error": str(exc),
            "counts": {},
            "active": [],
            "stalled_run_ids": [],
        }
    cutoff = time.time() - max(1, int(stall_after_seconds))
    stalled: list[str] = []
    active = [dict(row) for row in active_rows]
    for row in active:
        if row["status"] == "stalled":
            stalled.append(str(row["run_id"]))
            continue
        heartbeat = float(row.get("heartbeat_at") or 0.0)
        if heartbeat < cutoff or not JobStore._pid_alive(row.get("worker_pid")):
            stalled.append(str(row["run_id"]))
    return {
        "schema_version": JOB_SCHEMA_VERSION,
        "database": path.as_posix(),
        "exists": True,
        "counts": counts,
        "active": active,
        "stalled_run_ids": stalled,
    }
