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
    bot._pending_wipe_confirm = {}
    bot._pending_multi_plan_by_session = {}
    bot._pending_trusted_agent_run_by_session = {}
    bot._pending_voice_goal_by_session = {}
    bot._voice_request_ids_by_session = {}
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


@pytest.mark.asyncio
async def test_confirmed_global_wipe_revokes_pending_actions_across_chats():
    bot = LightClawBot.__new__(LightClawBot)
    bot.is_update_allowed = lambda _update: True
    bot._session_id_from_update = lambda _update: "current"
    bot._log_user_message = Mock()
    bot._pending_wipe_confirm = {"current": 9_999_999_999, "other": 9_999_999_999}
    bot._pending_multi_plan_by_session = {"current": {}, "other": {}}
    bot._pending_trusted_agent_run_by_session = {"current": {}, "other": {}}
    bot._pending_voice_goal_by_session = {"current": {}, "other": {}}
    bot._voice_request_ids_by_session = {"current": "a", "other": "b"}
    bot._active_run_tasks_by_session = {"other": asyncio.current_task()}
    bot._session_summaries = {"current": "old", "other": "old"}
    bot._invalidate_active_summaries = Mock()
    bot.memory = SimpleNamespace(clear_all=Mock())
    bot._reply_logged = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=42, type="private"),
        message=SimpleNamespace(),
    )

    await bot.cmd_wipe_memory(update, SimpleNamespace(args=["confirm"]))

    assert bot._pending_wipe_confirm == {}
    assert bot._pending_multi_plan_by_session == {}
    assert bot._pending_trusted_agent_run_by_session == {}
    assert bot._pending_voice_goal_by_session == {}
    assert bot._voice_request_ids_by_session == {}
    assert bot._active_run_tasks_by_session == {"other": asyncio.current_task()}
    assert bot._session_summaries == {}
    bot.memory.clear_all.assert_called_once()
    assert "Already-active runs continue" in bot._reply_logged.await_args.args[1]
