from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.bot.file_ops import BotFileOpsMixin
from memory import MemoryStore


class FileOpsHarness(BotFileOpsMixin):
    def __init__(self, workspace: Path):
        self.config = SimpleNamespace(workspace_path=str(workspace))


def test_workspace_path_blocks_parent_absolute_and_symlink_escape(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    harness = FileOpsHarness(workspace)

    assert harness._resolve_workspace_path("../outside/file.txt")[2]
    assert harness._resolve_workspace_path(str(outside / "file.txt"))[2]
    assert "symlink" in (harness._resolve_workspace_path("escape/file.txt")[2] or "")

    dangling_target = workspace / "redirect-target"
    dangling = workspace / "redirect"
    dangling.symlink_to(dangling_target, target_is_directory=True)
    target, _, error = harness._resolve_workspace_path("redirect/new.txt")
    assert target is None
    assert "symlink" in (error or "")
    assert not dangling_target.exists()

    target, relative, error = harness._resolve_workspace_path("safe/file.txt")
    assert error is None
    assert relative == "safe/file.txt"
    assert target == workspace / "safe" / "file.txt"


def test_memory_persists_and_recalls_lexical_matches(tmp_path):
    database = tmp_path / "memory.db"
    first = MemoryStore(str(database))
    first.ingest("user", "the deployment codename is amberfalcon", "session-a")
    first.db.close()

    second = MemoryStore(str(database))
    records = second.recall("amberfalcon deployment", top_k=3, session_id="session-a")

    assert records
    assert records[0].session_id == "session-a"
    assert "amberfalcon" in records[0].content
    second.db.close()


def test_memory_store_keeps_wal_sidecars_private_in_shared_parent(tmp_path):
    parent = tmp_path / "shared"
    parent.mkdir()
    parent.chmod(0o755)
    database = parent / "memory.db"
    legacy = sqlite3.connect(database)
    legacy.execute("PRAGMA journal_mode=WAL")
    legacy.execute("CREATE TABLE legacy (value TEXT)")
    legacy.execute("INSERT INTO legacy VALUES ('private state')")
    legacy.commit()
    os.chmod(database, 0o600)
    for suffix in ("-wal", "-shm"):
        os.chmod(database.with_name(database.name + suffix), 0o644)

    store = MemoryStore(str(database))
    store.ingest("user", "private interaction", "session-a")

    for path in (database, parent / "memory.db-wal", parent / "memory.db-shm"):
        assert path.exists()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    store.db.close()
    legacy.close()


def test_memory_store_fails_closed_when_database_cannot_be_private(tmp_path, monkeypatch):
    def fail_chmod(*_args, **_kwargs):
        raise PermissionError("fixture permission failure")

    monkeypatch.setattr("core.fs.os.fchmod", fail_chmod)
    with pytest.raises(PermissionError, match="fixture permission failure"):
        MemoryStore(str(tmp_path / "memory.db"))
