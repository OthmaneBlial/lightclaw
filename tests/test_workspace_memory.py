from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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
    records = second.recall("amberfalcon deployment", top_k=3)

    assert records
    assert records[0].session_id == "session-a"
    assert "amberfalcon" in records[0].content
    second.db.close()
