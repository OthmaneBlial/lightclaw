"""Private, namespaced SQLite FTS5 lexical memory with bounded growth."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import struct
import tempfile
import threading
import time
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

log = logging.getLogger("lightclaw.memory")

MEMORY_SCHEMA_VERSION = 2
DEFAULT_RETENTION_DAYS = 90
DEFAULT_MAX_INTERACTIONS = 10_000
DEFAULT_MAX_DB_BYTES = 64 * 1024 * 1024
DEFAULT_QUERY_TIMEOUT_MS = 100
DEFAULT_CANDIDATE_LIMIT = 200
MAX_QUERY_TERMS = 16
MAX_RECORD_CHARS = 32_000


def _tokenize(text: str) -> list[str]:
    """Return Unicode alphanumeric tokens for deliberately lexical recall."""
    return re.findall(r"[^\W_]+", str(text).lower(), flags=re.UNICODE)


def _private_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(raw_temp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        path.chmod(0o600)
    finally:
        temp.unlink(missing_ok=True)


@runtime_checkable
class EmbeddingAdapter(Protocol):
    """Optional local or remote adapter; FTS5 remains the required baseline."""

    name: str
    version: str

    def embed(self, text: str) -> Sequence[float]:
        """Return one finite numeric vector for text."""


@dataclass
class MemoryRecord:
    id: int
    timestamp: float
    role: str
    content: str
    session_id: str
    user_namespace: str = ""
    workspace_namespace: str = ""
    similarity: float = 0.0
    lexical_score: float = 0.0
    embedding_score: float | None = None


class MemoryStore:
    """Persistent local lexical recall scoped to one user and workspace.

    Retrieval uses SQLite FTS5 and inspects at most ``candidate_limit`` rows.
    Embeddings are optional and may only rerank those already bounded lexical
    candidates. They are never required for storage or recall.
    """

    def __init__(
        self,
        db_path: str = "lightclaw.db",
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        max_interactions: int = DEFAULT_MAX_INTERACTIONS,
        max_db_bytes: int = DEFAULT_MAX_DB_BYTES,
        query_timeout_ms: int = DEFAULT_QUERY_TIMEOUT_MS,
        candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
        embedding_adapter: EmbeddingAdapter | None = None,
    ):
        self.path = Path(db_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.retention_days = max(1, int(retention_days))
        self.max_interactions = max(100, int(max_interactions))
        self.max_db_bytes = max(1_048_576, int(max_db_bytes))
        self.query_timeout_ms = max(10, min(5_000, int(query_timeout_ms)))
        self.candidate_limit = max(10, min(2_000, int(candidate_limit)))
        self.embedding_adapter = embedding_adapter
        self._lock = threading.RLock()
        self._context_scopes: ContextVar[dict[str, tuple[str, str]] | None] = ContextVar(
            f"lightclaw_memory_scopes_{id(self)}",
            default=None,
        )
        self._last_query_ms = 0.0
        self._timed_out_queries = 0
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
        self.db.row_factory = sqlite3.Row
        self._init_db()
        self._enforce_retention()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _init_db(self) -> None:
        with self._lock:
            self.db.execute("PRAGMA foreign_keys = ON")
            self.db.execute("PRAGMA busy_timeout = 5000")
            self.db.execute("PRAGMA journal_mode = WAL")
            self.db.execute("PRAGMA wal_autocheckpoint = 100")
            self.db.execute("PRAGMA journal_size_limit = 4194304")
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS interactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    user_namespace TEXT NOT NULL DEFAULT '',
                    workspace_namespace TEXT NOT NULL DEFAULT '',
                    embedding BLOB
                );
                CREATE INDEX IF NOT EXISTS idx_interactions_session
                    ON interactions(session_id);

                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    summary TEXT DEFAULT '',
                    updated REAL
                );
                CREATE TABLE IF NOT EXISTS memory_scopes (
                    session_id TEXT PRIMARY KEY,
                    user_namespace TEXT NOT NULL,
                    workspace_namespace TEXT NOT NULL,
                    updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_summaries (
                    session_id TEXT NOT NULL,
                    user_namespace TEXT NOT NULL,
                    workspace_namespace TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    updated REAL NOT NULL,
                    PRIMARY KEY (session_id, user_namespace, workspace_namespace)
                );
                CREATE TABLE IF NOT EXISTS memory_embeddings (
                    interaction_id INTEGER NOT NULL REFERENCES interactions(id) ON DELETE CASCADE,
                    adapter TEXT NOT NULL,
                    version TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    vector BLOB NOT NULL,
                    PRIMARY KEY (interaction_id, adapter, version)
                );
                CREATE TABLE IF NOT EXISTS memory_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            columns = {
                str(row["name"])
                for row in self.db.execute("PRAGMA table_info(interactions)").fetchall()
            }
            if "user_namespace" not in columns:
                self.db.execute(
                    "ALTER TABLE interactions ADD COLUMN user_namespace TEXT NOT NULL DEFAULT ''"
                )
            if "workspace_namespace" not in columns:
                self.db.execute(
                    "ALTER TABLE interactions ADD COLUMN workspace_namespace TEXT NOT NULL DEFAULT ''"
                )
            self.db.execute(
                "UPDATE interactions SET user_namespace = 'legacy-session:' || session_id "
                "WHERE user_namespace = ''"
            )
            self.db.execute(
                "UPDATE interactions SET workspace_namespace = 'legacy-default' "
                "WHERE workspace_namespace = ''"
            )
            self.db.execute(
                "INSERT OR IGNORE INTO memory_summaries("
                "session_id, user_namespace, workspace_namespace, summary, updated"
                ") SELECT session_id, 'legacy-session:' || session_id, "
                "'legacy-default', summary, COALESCE(updated, 0) FROM sessions "
                "WHERE summary != ''"
            )
            self.db.execute(
                "INSERT INTO memory_meta(key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(MEMORY_SCHEMA_VERSION),),
            )
            try:
                self.db.executescript(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS interactions_fts
                    USING fts5(content, tokenize='unicode61');

                    CREATE TRIGGER IF NOT EXISTS interactions_fts_insert
                    AFTER INSERT ON interactions BEGIN
                        INSERT INTO interactions_fts(rowid, content) VALUES (new.id, new.content);
                    END;
                    CREATE TRIGGER IF NOT EXISTS interactions_fts_delete
                    AFTER DELETE ON interactions BEGIN
                        DELETE FROM interactions_fts WHERE rowid = old.id;
                    END;
                    CREATE TRIGGER IF NOT EXISTS interactions_fts_update
                    AFTER UPDATE OF content ON interactions BEGIN
                        DELETE FROM interactions_fts WHERE rowid = old.id;
                        INSERT INTO interactions_fts(rowid, content) VALUES (new.id, new.content);
                    END;
                    """
                )
            except sqlite3.OperationalError as exc:
                raise RuntimeError("LightClaw memory requires SQLite FTS5 support") from exc
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS idx_interactions_scope_time "
                "ON interactions(user_namespace, workspace_namespace, timestamp DESC)"
            )
            interaction_count = int(
                self.db.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]
            )
            fts_count = int(self.db.execute("SELECT COUNT(*) FROM interactions_fts").fetchone()[0])
            if interaction_count != fts_count:
                self.db.execute("DELETE FROM interactions_fts")
                self.db.execute(
                    "INSERT INTO interactions_fts(rowid, content) SELECT id, content FROM interactions"
                )
            self.db.commit()

    @staticmethod
    def _clean_namespace(value: str, label: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned or len(cleaned) > 500 or "\x00" in cleaned:
            raise ValueError(f"{label} must be a non-empty bounded string")
        return cleaned

    def bind_session(
        self,
        session_id: str,
        *,
        user_namespace: str,
        workspace_namespace: str,
    ) -> tuple[str, str]:
        """Persist the private scope used by future calls for this session."""
        session = self._clean_namespace(session_id, "session id")
        user = self._clean_namespace(user_namespace, "user namespace")
        workspace = self._clean_namespace(workspace_namespace, "workspace namespace")
        active = dict(self._context_scopes.get() or {})
        active[session] = (user, workspace)
        self._context_scopes.set(active)
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO memory_scopes(session_id, user_namespace, workspace_namespace, updated) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                "user_namespace = excluded.user_namespace, "
                "workspace_namespace = excluded.workspace_namespace, updated = excluded.updated",
                (session, user, workspace, time.time()),
            )
        return user, workspace

    def _scope_for(
        self,
        session_id: str | None,
        user_namespace: str | None = None,
        workspace_namespace: str | None = None,
    ) -> tuple[str, str]:
        if user_namespace is not None or workspace_namespace is not None:
            if user_namespace is None or workspace_namespace is None:
                raise ValueError("both user and workspace namespaces are required")
            return (
                self._clean_namespace(user_namespace, "user namespace"),
                self._clean_namespace(workspace_namespace, "workspace namespace"),
            )
        if not session_id:
            raise ValueError("a session id or explicit memory scope is required")
        session = self._clean_namespace(session_id, "session id")
        active = self._context_scopes.get() or {}
        if session in active:
            return active[session]
        with self._lock:
            row = self.db.execute(
                "SELECT user_namespace, workspace_namespace FROM memory_scopes WHERE session_id = ?",
                (session,),
            ).fetchone()
        if row:
            return str(row["user_namespace"]), str(row["workspace_namespace"])
        return f"legacy-session:{session}", "legacy-default"

    def _embedding_identity(self) -> tuple[str, str] | None:
        adapter = self.embedding_adapter
        if adapter is None:
            return None
        name = self._clean_namespace(getattr(adapter, "name", ""), "embedding adapter name")
        version = self._clean_namespace(
            getattr(adapter, "version", ""), "embedding adapter version"
        )
        return name, version

    @staticmethod
    def _pack_vector(values: Sequence[float]) -> tuple[int, bytes]:
        vector = [float(value) for value in values]
        if not vector or len(vector) > 8192 or not all(math.isfinite(value) for value in vector):
            raise ValueError("embedding vector must contain 1-8192 finite values")
        return len(vector), struct.pack(f"<{len(vector)}f", *vector)

    @staticmethod
    def _unpack_vector(raw: bytes, dimensions: int) -> tuple[float, ...]:
        if dimensions <= 0 or len(raw) != dimensions * 4:
            return ()
        return struct.unpack(f"<{dimensions}f", raw)

    @staticmethod
    def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
        if len(left) != len(right) or not left:
            return 0.0
        dot = sum(a * b for a, b in zip(left, right, strict=True))
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        if not left_norm or not right_norm:
            return 0.0
        return max(-1.0, min(1.0, dot / (left_norm * right_norm)))

    def ingest(
        self,
        role: str,
        content: str,
        session_id: str,
        *,
        user_namespace: str | None = None,
        workspace_namespace: str | None = None,
    ) -> int | None:
        """Save one bounded interaction in its exact user/workspace scope."""
        raw = str(content or "")
        if not raw.strip():
            return None
        stored = raw[:MAX_RECORD_CHARS]
        if len(raw) > MAX_RECORD_CHARS:
            stored += "\n[truncated by LightClaw memory limit]"
        user, workspace = self._scope_for(
            session_id,
            user_namespace,
            workspace_namespace,
        )
        with self._lock, self.db:
            cursor = self.db.execute(
                "INSERT INTO interactions("
                "timestamp, role, content, session_id, user_namespace, workspace_namespace"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), str(role), stored, str(session_id), user, workspace),
            )
            interaction_id = int(cursor.lastrowid)
            identity = self._embedding_identity()
            if identity and self.embedding_adapter:
                try:
                    dimensions, vector = self._pack_vector(self.embedding_adapter.embed(stored))
                    self.db.execute(
                        "INSERT INTO memory_embeddings("
                        "interaction_id, adapter, version, dimensions, vector"
                        ") VALUES (?, ?, ?, ?, ?)",
                        (interaction_id, identity[0], identity[1], dimensions, vector),
                    )
                except Exception as exc:
                    log.warning("Optional memory embedding failed; lexical record kept: %s", exc)
        self._enforce_retention()
        return interaction_id

    def recall(
        self,
        query: str,
        top_k: int = 5,
        exclude_session: str | None = None,
        *,
        session_id: str | None = None,
        user_namespace: str | None = None,
        workspace_namespace: str | None = None,
    ) -> list[MemoryRecord]:
        """Return bounded FTS5 lexical results from exactly one private scope."""
        terms = list(dict.fromkeys(_tokenize(query)))[:MAX_QUERY_TERMS]
        if not terms:
            return []
        user, workspace = self._scope_for(
            session_id,
            user_namespace,
            workspace_namespace,
        )
        match = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
        requested = max(1, min(50, int(top_k)))
        candidate_count = min(self.candidate_limit, max(25, requested * 8))
        params: list[object] = [match, user, workspace]
        session_clause = ""
        if exclude_session:
            session_clause = " AND i.session_id != ?"
            params.append(str(exclude_session))
        identity = self._embedding_identity()
        join = ""
        select_embedding = "NULL AS dimensions, NULL AS vector"
        if identity:
            join = (
                " LEFT JOIN memory_embeddings e ON e.interaction_id = i.id "
                "AND e.adapter = ? AND e.version = ?"
            )
            select_embedding = "e.dimensions AS dimensions, e.vector AS vector"
            params = [identity[0], identity[1], *params]
        params.append(candidate_count)
        sql = (
            "SELECT i.id, i.timestamp, i.role, i.content, i.session_id, "
            "i.user_namespace, i.workspace_namespace, bm25(interactions_fts) AS fts_rank, "
            f"{select_embedding} FROM interactions_fts "
            "JOIN interactions i ON i.id = interactions_fts.rowid"
            f"{join} WHERE interactions_fts MATCH ? "
            "AND i.user_namespace = ? AND i.workspace_namespace = ?"
            f"{session_clause} ORDER BY fts_rank, i.timestamp DESC LIMIT ?"
        )
        started = time.perf_counter()
        deadline = started + (self.query_timeout_ms / 1000)
        with self._lock:
            self.db.set_progress_handler(lambda: int(time.perf_counter() > deadline), 1_000)
            try:
                rows = self.db.execute(sql, params).fetchall()
            except sqlite3.OperationalError as exc:
                if "interrupted" in str(exc).lower():
                    self._timed_out_queries += 1
                    return []
                raise
            finally:
                self.db.set_progress_handler(None, 0)
                self._last_query_ms = round((time.perf_counter() - started) * 1_000, 3)

        query_tokens = set(terms)
        query_vector: Sequence[float] | None = None
        if identity and self.embedding_adapter:
            try:
                query_vector = tuple(float(value) for value in self.embedding_adapter.embed(query))
            except Exception as exc:
                log.warning("Optional memory query embedding failed; using lexical rank: %s", exc)
        scored: list[MemoryRecord] = []
        for row in rows:
            content_tokens = set(_tokenize(str(row["content"])))
            overlap = len(query_tokens & content_tokens)
            coverage = overlap / max(1, len(query_tokens))
            specificity = overlap / max(1, len(content_tokens))
            lexical = min(1.0, (0.85 * coverage) + (0.15 * specificity))
            embedding_score: float | None = None
            combined = lexical
            if query_vector is not None and row["vector"] is not None:
                stored = self._unpack_vector(bytes(row["vector"]), int(row["dimensions"] or 0))
                if stored and len(stored) == len(query_vector):
                    embedding_score = max(0.0, self._cosine(query_vector, stored))
                    combined = (0.75 * lexical) + (0.25 * embedding_score)
            scored.append(
                MemoryRecord(
                    id=int(row["id"]),
                    timestamp=float(row["timestamp"]),
                    role=str(row["role"]),
                    content=str(row["content"]),
                    session_id=str(row["session_id"]),
                    user_namespace=str(row["user_namespace"]),
                    workspace_namespace=str(row["workspace_namespace"]),
                    similarity=combined,
                    lexical_score=lexical,
                    embedding_score=embedding_score,
                )
            )
        scored.sort(key=lambda record: (record.similarity, record.timestamp), reverse=True)
        return scored[:requested]

    def get_recent(self, session_id: str, limit: int = 20) -> list[dict[str, str]]:
        """Get chronological recent messages without crossing the bound scope."""
        user, workspace = self._scope_for(session_id)
        with self._lock:
            rows = self.db.execute(
                "SELECT role, content FROM interactions WHERE session_id = ? "
                "AND user_namespace = ? AND workspace_namespace = ? "
                "ORDER BY timestamp DESC LIMIT ?",
                (str(session_id), user, workspace, max(1, min(500, int(limit)))),
            ).fetchall()
        return [
            {"role": str(row["role"]), "content": str(row["content"])}
            for row in reversed(rows)
        ]

    def get_summary(self, session_id: str) -> str:
        user, workspace = self._scope_for(session_id)
        row = self.db.execute(
            "SELECT summary FROM memory_summaries WHERE session_id = ? "
            "AND user_namespace = ? AND workspace_namespace = ?",
            (str(session_id), user, workspace),
        ).fetchone()
        return str(row["summary"]) if row else ""

    def set_summary(self, session_id: str, summary: str) -> None:
        user, workspace = self._scope_for(session_id)
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO memory_summaries("
                "session_id, user_namespace, workspace_namespace, summary, updated"
                ") VALUES (?, ?, ?, ?, ?) ON CONFLICT("
                "session_id, user_namespace, workspace_namespace"
                ") DO UPDATE SET summary = excluded.summary, updated = excluded.updated",
                (str(session_id), user, workspace, str(summary), time.time()),
            )

    def _database_bytes(self) -> int:
        return sum(
            path.stat().st_size
            for path in (
                self.path,
                Path(f"{self.path}-wal"),
                Path(f"{self.path}-shm"),
            )
            if path.is_file()
        )

    def _retention_ids(self) -> tuple[list[int], dict[str, int]]:
        cutoff = time.time() - (self.retention_days * 86_400)
        expired = [
            int(row[0])
            for row in self.db.execute(
                "SELECT id FROM interactions WHERE timestamp < ? ORDER BY timestamp",
                (cutoff,),
            ).fetchall()
        ]
        total = int(self.db.execute("SELECT COUNT(*) FROM interactions").fetchone()[0])
        overflow_count = max(0, total - self.max_interactions)
        overflow = [
            int(row[0])
            for row in self.db.execute(
                "SELECT id FROM interactions ORDER BY timestamp, id LIMIT ?",
                (overflow_count,),
            ).fetchall()
        ]
        identifiers = list(dict.fromkeys([*expired, *overflow]))
        return identifiers, {"expired": len(expired), "overflow": len(overflow)}

    def prune(self, *, apply: bool = False) -> dict[str, object]:
        """Preview or enforce age/count retention and the physical size ceiling."""
        with self._lock:
            identifiers, reasons = self._retention_ids()
            before = self._database_bytes()
            result: dict[str, object] = {
                "applied": False,
                "records_to_delete": len(identifiers),
                "reasons": reasons,
                "database_bytes_before": before,
                "max_database_bytes": self.max_db_bytes,
            }
            if not apply:
                return result
            if not identifiers and before <= self.max_db_bytes:
                result["applied"] = True
                result["database_bytes_after"] = before
                result["remaining_interactions"] = int(
                    self.db.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]
                )
                return result
            if identifiers:
                placeholders = ",".join("?" for _ in identifiers)
                with self.db:
                    self.db.execute(
                        f"DELETE FROM interactions WHERE id IN ({placeholders})",
                        identifiers,
                    )
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            if before > self.max_db_bytes or identifiers:
                self.db.execute("VACUUM")
            while self._database_bytes() > self.max_db_bytes:
                oldest = self.db.execute(
                    "SELECT id FROM interactions ORDER BY timestamp, id LIMIT 100"
                ).fetchall()
                if not oldest:
                    break
                with self.db:
                    self.db.executemany(
                        "DELETE FROM interactions WHERE id = ?",
                        [(int(row[0]),) for row in oldest],
                    )
                self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self.db.execute("VACUUM")
            result["applied"] = True
            result["database_bytes_after"] = self._database_bytes()
            result["remaining_interactions"] = int(
                self.db.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]
            )
            return result

    def _enforce_retention(self) -> None:
        self.prune(apply=True)

    def stats(
        self,
        *,
        session_id: str | None = None,
        user_namespace: str | None = None,
        workspace_namespace: str | None = None,
    ) -> dict[str, object]:
        """Return truthful global or exact-scope memory statistics."""
        where = ""
        params: tuple[object, ...] = ()
        scoped = session_id is not None or user_namespace is not None or workspace_namespace is not None
        if scoped:
            user, workspace = self._scope_for(
                session_id,
                user_namespace,
                workspace_namespace,
            )
            where = " WHERE user_namespace = ? AND workspace_namespace = ?"
            params = (user, workspace)
        total = int(
            self.db.execute(f"SELECT COUNT(*) FROM interactions{where}", params).fetchone()[0]
        )
        sessions = int(
            self.db.execute(
                f"SELECT COUNT(DISTINCT session_id) FROM interactions{where}", params
            ).fetchone()[0]
        )
        return {
            "schema_version": MEMORY_SCHEMA_VERSION,
            "retrieval": "sqlite-fts5-lexical",
            "embedding_adapter": "/".join(self._embedding_identity() or ()) or None,
            "total_interactions": total,
            "unique_sessions": sessions,
            "database_bytes": self._database_bytes(),
            "max_database_bytes": self.max_db_bytes,
            "retention_days": self.retention_days,
            "max_interactions": self.max_interactions,
            "query_timeout_ms": self.query_timeout_ms,
            "candidate_limit": self.candidate_limit,
            "last_query_ms": self._last_query_ms,
            "timed_out_queries": self._timed_out_queries,
            "scoped": scoped,
        }

    def export_scope(
        self,
        output_path: str | Path,
        *,
        session_id: str | None = None,
        user_namespace: str | None = None,
        workspace_namespace: str | None = None,
        apply: bool = False,
    ) -> dict[str, object]:
        """Preview or write an owner-only JSON export of one exact scope."""
        user, workspace = self._scope_for(
            session_id,
            user_namespace,
            workspace_namespace,
        )
        rows = self.db.execute(
            "SELECT id, timestamp, role, content, session_id FROM interactions "
            "WHERE user_namespace = ? AND workspace_namespace = ? ORDER BY timestamp, id",
            (user, workspace),
        ).fetchall()
        destination = Path(output_path).expanduser().resolve()
        payload = {
            "schema_version": MEMORY_SCHEMA_VERSION,
            "retrieval": "sqlite-fts5-lexical",
            "user_namespace": user,
            "workspace_namespace": workspace,
            "records": [dict(row) for row in rows],
        }
        result: dict[str, object] = {
            "applied": False,
            "output": destination.as_posix(),
            "record_count": len(rows),
            "included_fields": ["id", "timestamp", "role", "content", "session_id"],
        }
        if apply:
            _private_write(destination, json.dumps(payload, indent=2, sort_keys=True) + "\n")
            result["applied"] = True
        return result

    def delete_records(
        self,
        record_ids: Sequence[int],
        *,
        session_id: str | None = None,
        user_namespace: str | None = None,
        workspace_namespace: str | None = None,
        apply: bool = False,
    ) -> dict[str, object]:
        """Preview or selectively delete IDs only inside one exact scope."""
        identifiers = sorted({int(value) for value in record_ids if int(value) > 0})[:500]
        if not identifiers:
            raise ValueError("at least one positive memory record id is required")
        user, workspace = self._scope_for(
            session_id,
            user_namespace,
            workspace_namespace,
        )
        placeholders = ",".join("?" for _ in identifiers)
        params: list[object] = [*identifiers, user, workspace]
        found = [
            int(row[0])
            for row in self.db.execute(
                f"SELECT id FROM interactions WHERE id IN ({placeholders}) "
                "AND user_namespace = ? AND workspace_namespace = ? ORDER BY id",
                params,
            ).fetchall()
        ]
        result: dict[str, object] = {
            "applied": False,
            "requested_ids": identifiers,
            "matched_ids": found,
            "scope": {"user_namespace": user, "workspace_namespace": workspace},
        }
        if apply and found:
            delete_placeholders = ",".join("?" for _ in found)
            with self._lock, self.db:
                self.db.execute(
                    f"DELETE FROM interactions WHERE id IN ({delete_placeholders})",
                    found,
                )
            result["applied"] = True
        elif apply:
            result["applied"] = True
        return result

    def clear_scope(
        self,
        *,
        session_id: str | None = None,
        user_namespace: str | None = None,
        workspace_namespace: str | None = None,
        apply: bool = False,
    ) -> dict[str, object]:
        """Preview or delete all interactions and summaries for one exact scope."""
        user, workspace = self._scope_for(
            session_id,
            user_namespace,
            workspace_namespace,
        )
        count = int(
            self.db.execute(
                "SELECT COUNT(*) FROM interactions WHERE user_namespace = ? "
                "AND workspace_namespace = ?",
                (user, workspace),
            ).fetchone()[0]
        )
        result: dict[str, object] = {
            "applied": False,
            "record_count": count,
            "scope": {"user_namespace": user, "workspace_namespace": workspace},
        }
        if apply:
            with self._lock, self.db:
                self.db.execute(
                    "DELETE FROM interactions WHERE user_namespace = ? "
                    "AND workspace_namespace = ?",
                    (user, workspace),
                )
                self.db.execute(
                    "DELETE FROM memory_summaries WHERE user_namespace = ? "
                    "AND workspace_namespace = ?",
                    (user, workspace),
                )
            result["applied"] = True
        return result

    def clear_session(self, session_id: str) -> None:
        user, workspace = self._scope_for(session_id)
        with self._lock, self.db:
            self.db.execute(
                "DELETE FROM interactions WHERE session_id = ? AND user_namespace = ? "
                "AND workspace_namespace = ?",
                (str(session_id), user, workspace),
            )
            self.db.execute(
                "DELETE FROM memory_summaries WHERE session_id = ? AND user_namespace = ? "
                "AND workspace_namespace = ?",
                (str(session_id), user, workspace),
            )

    def delete_delegation_transcripts(self, session_id: str) -> int:
        user, workspace = self._scope_for(session_id)
        with self._lock, self.db:
            cursor = self.db.execute(
                "DELETE FROM interactions WHERE session_id = ? AND user_namespace = ? "
                "AND workspace_namespace = ? AND role = 'assistant' AND content LIKE ?",
                (str(session_id), user, workspace, "🤖 Delegated to %"),
            )
        return int(cursor.rowcount or 0)

    def clear_all(self) -> None:
        with self._lock, self.db:
            self.db.execute("DELETE FROM interactions")
            self.db.execute("DELETE FROM memory_summaries")
            self.db.execute("DELETE FROM sessions")

    def format_memories_for_prompt(self, memories: list[MemoryRecord]) -> str:
        if not memories:
            return ""
        lines = ["## Recalled lexical memories", ""]
        for memory in memories:
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(memory.timestamp))
            lines.append(f"- [{stamp}] {memory.role}: {memory.content[:200]}")
        return "\n".join(lines)
