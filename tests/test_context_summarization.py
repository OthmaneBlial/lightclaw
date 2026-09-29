from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from config import Config
from core.bot import LightClawBot


@pytest.mark.asyncio
async def test_clear_during_summary_does_not_restore_old_context():
    started = asyncio.Event()
    finish = asyncio.Event()

    async def summarize(*args, **kwargs):
        started.set()
        await finish.wait()
        return "summary from erased conversation"

    bot = LightClawBot.__new__(LightClawBot)
    bot.config = Config(telegram_allowed_users=["42"], context_window=128)
    bot.memory = SimpleNamespace(
        get_recent=Mock(
            return_value=[
                {"role": "user", "content": f"old detail {index}"}
                for index in range(21)
            ]
        ),
        clear_session=Mock(),
    )
    bot.llm = SimpleNamespace(chat=AsyncMock(side_effect=summarize))
    bot._llm_backoff_until = 0.0
    bot._summarizing = set()
    bot._session_summaries = {}
    bot._summary_generation_by_session = {}
    bot._session_id_from_update = Mock(return_value="chat-42")
    bot._log_user_message = Mock()
    bot._privileged_rate_limited = Mock(return_value=False)
    bot._reply_logged = AsyncMock()

    task = asyncio.create_task(bot.maybe_summarize("chat-42"))
    await started.wait()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=42, type="private"),
        message=SimpleNamespace(),
    )
    try:
        await bot.cmd_clear(update, SimpleNamespace())
    finally:
        finish.set()
    await task

    assert "chat-42" not in bot._session_summaries
    bot.memory.clear_session.assert_called_once_with("chat-42")
