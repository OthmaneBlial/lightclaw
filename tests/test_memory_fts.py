from __future__ import annotations

import asyncio
import json
import sqlite3
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import pytest

from lightclaw_cli import build_parser
from memory import MAX_RECORD_CHARS, MemoryStore


class FixtureEmbeddingAdapter:
    name = "fixture-semantic-map"
    version = "1"

    def embed(self, text: str):
        lowered = text.lower()
        if "cat" in lowered or "question" in lowered:
            return [1.0, 0.0]
        if "dog" in lowered:
            return [0.0, 1.0]
        return [0.5, 0.5]


def test_memory_store_rejects_preexisting_db_symlink_without_changing_target(tmp_path):
    database = tmp_path / "lightclaw.db"
    victim = tmp_path / "victim.db"
    legacy = sqlite3.connect(victim)
    legacy.execute("CREATE TABLE private_data (value TEXT)")
    legacy.execute("INSERT INTO private_data VALUES ('preserve this data')")
    legacy.commit()
    legacy.close()
    victim.chmod(0o644)
    original = victim.read_bytes()
    database.symlink_to(victim)

    with pytest.raises(OSError):
        MemoryStore(str(database))

    assert victim.read_bytes() == original
    assert stat.S_IMODE(victim.stat().st_mode) == 0o644


def test_fts5_lexical_recall_isolated_by_user_workspace_and_summary(tmp_path):
    store = MemoryStore(str(tmp_path / "memory.db"))
    store.bind_session(
        "shared-chat",
        user_namespace="telegram-user:1",
        workspace_namespace="/workspace/a",
    )
    first_id = store.ingest("user", "private cobalt launch code", "shared-chat")
    store.set_summary("shared-chat", "user one summary")

    store.bind_session(
        "shared-chat",
        user_namespace="telegram-user:2",
        workspace_namespace="/workspace/a",
    )
    second_id = store.ingest("user", "private amber launch code", "shared-chat")
    store.set_summary("shared-chat", "user two summary")

    current = store.recall("private launch code", session_id="shared-chat", top_k=5)
    first_user = store.recall(
        "private launch code",
        user_namespace="telegram-user:1",
        workspace_namespace="/workspace/a",
        top_k=5,
    )
    wrong_workspace = store.recall(
        "private launch code",
        user_namespace="telegram-user:1",
        workspace_namespace="/workspace/b",
        top_k=5,
    )

    assert [record.id for record in current] == [second_id]
    assert [record.id for record in first_user] == [first_id]
    assert all(record.user_namespace == "telegram-user:1" for record in first_user)
    assert wrong_workspace == []
    assert store.get_summary("shared-chat") == "user two summary"
    with pytest.raises(ValueError, match="session id or explicit"):
        store.recall("private launch code")
    store.db.close()


def test_current_session_only_recall_and_stats_exclude_private_chat_history(tmp_path):
    store = MemoryStore(str(tmp_path / "memory.db"))
    scope = {
        "user_namespace": "telegram-user:42",
        "workspace_namespace": "/workspace",
    }
    store.ingest("user", "private birch phrase", "private-chat", **scope)
    group_id = store.ingest("user", "group birch phrase", "group-chat", **scope)
    store.bind_session("group-chat", **scope)

    cross_chat = store.recall("birch phrase", session_id="group-chat")
    group_only = store.recall(
        "birch phrase", session_id="group-chat", current_session_only=True
    )
    all_stats = store.stats(session_id="group-chat")
    group_stats = store.stats(session_id="group-chat", current_session_only=True)

    assert {record.session_id for record in cross_chat} == {"private-chat", "group-chat"}
    assert [record.id for record in group_only] == [group_id]
    assert all_stats["total_interactions"] == 2
    assert group_stats["total_interactions"] == 1
    assert group_stats["unique_sessions"] == 1
    with pytest.raises(ValueError, match="current-session recall requires"):
        store.recall("birch phrase", current_session_only=True)
    with pytest.raises(ValueError, match="current-session stats require"):
        store.stats(current_session_only=True)
    store.db.close()


def test_concurrent_same_chat_bindings_do_not_cross_async_contexts(tmp_path):
    store = MemoryStore(str(tmp_path / "memory.db"))
    ready = asyncio.Event()
    bound = 0

    async def write_for(user: str, fact: str):
        nonlocal bound
        store.bind_session(
            "group-chat",
            user_namespace=user,
            workspace_namespace="shared-workspace",
        )
        bound += 1
        if bound == 2:
            ready.set()
        await ready.wait()
        store.ingest("user", fact, "group-chat")
        return store.recall("private code", session_id="group-chat", top_k=5)

    async def run_concurrent():
        return await asyncio.gather(
            write_for("telegram-user:1", "private code cobalt"),
            write_for("telegram-user:2", "private code amber"),
        )

    first, second = asyncio.run(run_concurrent())

    assert [record.content for record in first] == ["private code cobalt"]
    assert [record.content for record in second] == ["private code amber"]
    store.db.close()


def test_memory_reads_wait_for_scope_clear_transaction(tmp_path):
    store = MemoryStore(str(tmp_path / "memory.db"))
    scope = {"user_namespace": "fixture-user", "workspace_namespace": "fixture-workspace"}
    store.bind_session("shared-chat", **scope)
    store.ingest("user", "private fixture", "shared-chat")
    store.set_summary("shared-chat", "summary before clear")
    clear_paused = threading.Event()
    resume_clear = threading.Event()
    partial_read = threading.Event()
    connection = store.db

    class PausingConnection:
        def __getattr__(self, name):
            return getattr(connection, name)

        def __enter__(self):
            connection.__enter__()
            return self

        def __exit__(self, *args):
            return connection.__exit__(*args)

        def execute(self, sql, *args):
            if sql.startswith("DELETE FROM memory_summaries"):
                clear_paused.set()
                if not resume_clear.wait(3):
                    raise TimeoutError("scope-clear fixture timed out")
            elif clear_paused.is_set() and not resume_clear.is_set() and sql.startswith("SELECT"):
                partial_read.set()
            return connection.execute(sql, *args)

    store.db = PausingConnection()
    read_started = threading.Event()
    read_context = copy_context()

    def snapshot_scope():
        return store.stats(**scope)["total_interactions"], store.get_summary("shared-chat")

    def read_scope():
        read_started.set()
        return read_context.run(snapshot_scope)

    with ThreadPoolExecutor(max_workers=2) as pool:
        clearing = pool.submit(store.clear_scope, **scope, apply=True)
        reading = None
        try:
            assert clear_paused.wait(2)
            reading = pool.submit(read_scope)
            assert read_started.wait(2)
            assert not partial_read.wait(0.2)
        finally:
            resume_clear.set()
        clearing.result(timeout=2)
        assert reading is not None
        assert reading.result(timeout=2) == (0, "")
    store.db.close()


def test_unicode_fts_and_optional_embedding_reranking_are_bounded(tmp_path):
    store = MemoryStore(
        str(tmp_path / "memory.db"),
        candidate_limit=25,
        embedding_adapter=FixtureEmbeddingAdapter(),
    )
    scope = {
        "user_namespace": "fixture-user",
        "workspace_namespace": "fixture-workspace",
    }
    cat_id = store.ingest("user", "common animal cat قطة", "one", **scope)
    store.ingest("user", "common animal dog كلب", "two", **scope)

    results = store.recall("common animal question", top_k=2, **scope)
    arabic = store.recall("قطة", top_k=1, **scope)
    stats = store.stats(**scope)

    assert results[0].id == cat_id
    assert results[0].embedding_score == pytest.approx(1.0)
    assert arabic[0].id == cat_id
    assert stats["retrieval"] == "sqlite-fts5-lexical"
    assert stats["embedding_adapter"] == "fixture-semantic-map/1"
    assert stats["candidate_limit"] == 25
    assert float(stats["last_query_ms"]) <= float(stats["query_timeout_ms"]) * 5
    store.db.close()


def test_export_selective_delete_clear_and_private_permissions(tmp_path):
    store = MemoryStore(str(tmp_path / "memory.db"))
    scope_a = {"user_namespace": "user-a", "workspace_namespace": "workspace-a"}
    scope_b = {"user_namespace": "user-b", "workspace_namespace": "workspace-a"}
    selected = store.ingest("user", "export this exact record", "a", **scope_a)
    preserved = store.ingest("user", "preserve other scope", "b", **scope_b)
    destination = tmp_path / "exports" / "memory.json"

    preview = store.export_scope(destination, **scope_a)
    assert preview["applied"] is False
    assert preview["record_count"] == 1
    assert not destination.exists()
    applied = store.export_scope(destination, apply=True, **scope_a)
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert applied["applied"] is True
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert [record["id"] for record in payload["records"]] == [selected]

    wrong_scope = store.delete_records([selected], **scope_b)
    assert wrong_scope["matched_ids"] == []
    deletion = store.delete_records([selected, preserved], apply=True, **scope_a)
    assert deletion["matched_ids"] == [selected]
    assert store.recall("export exact", top_k=3, **scope_a) == []
    assert store.recall("preserve scope", top_k=3, **scope_b)[0].id == preserved

    clear_preview = store.clear_scope(**scope_b)
    assert clear_preview["record_count"] == 1
    assert store.clear_scope(apply=True, **scope_b)["applied"] is True
    assert store.stats(**scope_b)["total_interactions"] == 0
    store.db.close()


def test_retention_record_bounds_truncation_and_prune_preview(tmp_path):
    store = MemoryStore(
        str(tmp_path / "memory.db"),
        retention_days=1,
        max_interactions=100,
        max_db_bytes=2 * 1024 * 1024,
    )
    scope = {"user_namespace": "bounded", "workspace_namespace": "bounded"}
    long_id = store.ingest("user", "x" * (MAX_RECORD_CHARS + 500), "long", **scope)
    for index in range(105):
        store.ingest("user", f"bounded fixture {index}", f"session-{index}", **scope)
    stored = store.db.execute(
        "SELECT content FROM interactions WHERE id = ?", (long_id,)
    ).fetchone()
    assert stored is None  # Oldest record was removed by the 100-row bound.
    assert store.stats()["total_interactions"] == 100

    oldest_id = int(
        store.db.execute("SELECT id FROM interactions ORDER BY timestamp LIMIT 1").fetchone()[0]
    )
    store.db.execute(
        "UPDATE interactions SET timestamp = ? WHERE id = ?",
        (time.time() - 172_800, oldest_id),
    )
    store.db.commit()
    preview = store.prune()
    assert preview["applied"] is False
    assert preview["reasons"]["expired"] == 1
    applied = store.prune(apply=True)
    assert applied["remaining_interactions"] == 99
    assert int(applied["database_bytes_after"]) <= 2 * 1024 * 1024
    store.db.close()


def test_prune_expires_old_session_summaries(tmp_path):
    store = MemoryStore(tmp_path / "memory.db", retention_days=1)
    store.bind_session(
        "old", user_namespace="telegram-user:1", workspace_namespace="/repo"
    )
    store.set_summary("old", "expired context")
    store.bind_session(
        "recent", user_namespace="telegram-user:1", workspace_namespace="/repo"
    )
    store.set_summary("recent", "current context")
    store.db.execute(
        "UPDATE memory_summaries SET updated = ? WHERE session_id = ?",
        (time.time() - 172_800, "old"),
    )
    store.db.commit()

    preview = store.prune()
    assert preview["reasons"]["expired_summaries"] == 1
    assert preview["records_to_delete"] == 1
    assert store.prune(apply=True)["applied"] is True
    assert store.get_summary("old") == ""
    assert store.get_summary("recent") == "current context"
    store.db.close()


def test_database_size_prune_discards_derived_summary_before_interactions(tmp_path):
    store = MemoryStore(tmp_path / "memory.db", max_db_bytes=1_048_576)
    store.bind_session(
        "large", user_namespace="telegram-user:1", workspace_namespace="/repo"
    )
    store.ingest("user", "keep original conversation", "large")

    store.set_summary("large", "x" * 1_200_000)

    assert store.get_summary("large") == ""
    assert store.get_recent("large")[0]["content"] == "keep original conversation"
    assert store._database_bytes() <= store.max_db_bytes
    store.db.close()


def test_record_content_is_bounded_before_storage(tmp_path):
    store = MemoryStore(str(tmp_path / "memory.db"))
    identifier = store.ingest("user", "z" * (MAX_RECORD_CHARS + 100), "bounded")
    content = str(
        store.db.execute("SELECT content FROM interactions WHERE id = ?", (identifier,)).fetchone()[0]
    )
    assert content.startswith("z" * MAX_RECORD_CHARS)
    assert content.endswith("[truncated by LightClaw memory limit]")
    store.db.close()


def test_legacy_database_migrates_to_isolated_fts_scope(tmp_path):
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE interactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            session_id TEXT NOT NULL,
            embedding BLOB
        );
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            summary TEXT DEFAULT '',
            updated REAL
        );
        INSERT INTO interactions(timestamp, role, content, session_id)
        VALUES (1, 'user', 'legacy opal migration fact', 'old-session');
        INSERT INTO sessions(session_id, summary, updated)
        VALUES ('old-session', 'legacy summary', 1);
        """
    )
    recent = time.time()
    legacy.execute("UPDATE interactions SET timestamp = ?", (recent,))
    legacy.execute("UPDATE sessions SET updated = ?", (recent,))
    legacy.commit()
    legacy.close()

    migrated = MemoryStore(str(path))
    results = migrated.recall("opal migration", session_id="old-session")

    assert results[0].content == "legacy opal migration fact"
    assert results[0].user_namespace == "legacy-session:old-session"
    assert migrated.get_summary("old-session") == "legacy summary"
    assert migrated.stats()["schema_version"] == 2
    migrated.db.close()


def test_memory_cli_is_preview_first():
    parser = build_parser()
    export = parser.parse_args(
        ["memory", "export", "--session", "cli", "--output", "memory.json"]
    )
    deletion = parser.parse_args(["memory", "delete", "--ids", "1", "2"])

    assert export.memory_action == "export"
    assert export.apply is False
    assert deletion.ids == [1, 2]
    assert deletion.apply is False
