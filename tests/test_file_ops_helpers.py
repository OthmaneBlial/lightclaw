from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.bot import LightClawBot
from core.bot.file_ops import BotFileOpsMixin
from core.types import FileOperationResult


def _hunk(search, replace):
    return f"<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE"


@pytest.mark.parametrize(
    ("content", "edit_body", "expected"),
    [
        ("before old after", _hunk("old", "new"), ("before new after", None)),
        (
            "old",
            _hunk("old", "new") + "\n" + _hunk("new", "final"),
            ("final", None),
        ),
        (
            "keep original",
            _hunk("absent", "new"),
            ("keep original", "hunk 1: SEARCH text not found (must match exactly)"),
        ),
        (
            "same same",
            _hunk("same", "new"),
            ("same same", "hunk 1: SEARCH text appears 2 times; add more context"),
        ),
        ("original", _hunk("", "insert"), ("original", "hunk 1: SEARCH block is empty")),
        (
            "original",
            _hunk("original", "changed") + "\n" + _hunk("absent", "later"),
            ("original", "hunk 2: SEARCH text not found (must match exactly)"),
        ),
    ],
)
def test_search_replace_hunk_contracts(content, edit_body, expected):
    assert BotFileOpsMixin._apply_search_replace_hunks(content, edit_body) == expected


def test_diff_stats_ignore_file_headers():
    diff = "--- a/file.py\n+++ b/file.py\n@@ -1 +1,2 @@\n-old\n+new\n+added"
    assert BotFileOpsMixin._diff_line_stats(diff) == (2, 1)


def test_response_compaction_keeps_plain_text_and_removes_markers_and_code():
    response = "[File updated: app.py]\nImplemented the fix.\n\n```python\nprint('hidden')\n```\n\nNext step."
    assert BotFileOpsMixin._compact_response_for_file_ops(response) == (
        "Implemented the fix.\n\nNext step."
    )
    assert BotFileOpsMixin._compact_response_for_file_ops("[No changes: app.py]") == "Done."


def test_chat_fence_stripping_keeps_surrounding_text():
    response = "Before\n```python\nprint('hidden')\n```\nAfter"
    assert BotFileOpsMixin._strip_fenced_code_for_chat(response) == "Before\n\nAfter"
    assert BotFileOpsMixin._strip_fenced_code_for_chat("plain reply") == "plain reply"


@pytest.mark.parametrize(
    ("text", "incomplete"),
    [
        ("<!doctype html><html><body>ok</body></html>", False),
        ("<html><body>missing html close</body>", True),
        ("<html>missing body and html closes", True),
        ("ordinary text with <b>inline HTML</b>", False),
    ],
)
def test_incomplete_html_detection(text, incomplete):
    assert BotFileOpsMixin._is_incomplete_html_text(text) is incomplete


@pytest.mark.asyncio
async def test_credential_files_and_contents_never_enter_automatic_edit_context(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("OPENAI_API_KEY=env-secret\n", encoding="utf-8")
    (workspace / "settings.json").write_text(
        '{"apiKey": "json-secret"}\n', encoding="utf-8"
    )
    (workspace / "README.md").write_text("safe context\n", encoding="utf-8")
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(workspace_path=str(workspace))
    bot._last_file_by_session = {}
    bot.llm = SimpleNamespace(chat=AsyncMock(return_value=""))

    await bot._force_file_ops_pass("chat-1", "modify the app", "No changes yet.")

    prompt = bot.llm.chat.await_args.args[0][0]["content"]
    assert "safe context" in prompt
    assert "env-secret" not in prompt
    assert "json-secret" not in prompt
    assert '"apiKey"' not in prompt


@pytest.mark.asyncio
async def test_model_file_blocks_cannot_overwrite_env_files(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env_file = workspace / ".env"
    env_file.write_text("OPENAI_API_KEY=keep-me\n", encoding="utf-8")
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(workspace_path=str(workspace))

    operations, _ = await bot._process_file_blocks(
        "```env:.env\nOPENAI_API_KEY=replacement\n```",
        allow_file_writes=True,
    )

    assert len(operations) == 1
    assert operations[0].action == "error"
    assert "credential-sensitive" in operations[0].detail
    assert env_file.read_text(encoding="utf-8") == "OPENAI_API_KEY=keep-me\n"


@pytest.mark.asyncio
async def test_model_file_writes_are_atomic_and_preserve_existing_mode(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "app.py"
    source.write_text("print('original')\n", encoding="utf-8")
    source.chmod(0o750)
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(workspace_path=str(workspace))

    operations, _ = await bot._process_file_blocks(
        "```python:app.py\nprint('updated')\n```",
        allow_file_writes=True,
    )

    assert operations[0].action == "updated"
    assert source.read_text(encoding="utf-8") == "print('updated')"
    assert source.stat().st_mode & 0o777 == 0o750

    def fail_replace(_temporary, _destination):
        raise OSError("fixture replace failure")

    monkeypatch.setattr("core.fs.os.replace", fail_replace)
    operations, _ = await bot._process_file_blocks(
        "```python:app.py\nprint('partial')\n```",
        allow_file_writes=True,
    )

    assert operations[0].action == "error"
    assert source.read_text(encoding="utf-8") == "print('updated')"
    assert source.stat().st_mode & 0o777 == 0o750
    assert not list(workspace.glob(".app.py.*.tmp"))


@pytest.mark.asyncio
async def test_failed_sensitive_edit_is_not_retried_with_file_contents(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("OPENAI_API_KEY=env-secret\n", encoding="utf-8")
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(workspace_path=str(workspace))
    bot.llm = SimpleNamespace(chat=AsyncMock())

    operations, cleaned = await bot._retry_failed_edits(
        "update .env",
        "failed edit",
        [FileOperationResult("error", ".env", "SEARCH text not found")],
    )

    assert operations == []
    assert cleaned == ""
    bot.llm.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_html_repair_does_not_resend_detectable_credentials(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "index.html").write_text(
        "<html><body>API_KEY=html-secret", encoding="utf-8"
    )
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(workspace_path=str(workspace))
    bot.llm = SimpleNamespace(chat=AsyncMock())

    repairs = await bot._repair_incomplete_html(
        "chat-1", "repair the page", [FileOperationResult("created", "index.html")]
    )

    assert len(repairs) == 1
    assert repairs[0].action == "error"
    assert "may contain credentials" in repairs[0].detail
    bot.llm.chat.assert_not_awaited()
