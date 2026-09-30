from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.bot import LightClawBot


@pytest.mark.asyncio
async def test_messages_serialize_per_chat_without_blocking_other_chats():
    bot = LightClawBot.__new__(LightClawBot)
    bot._session_id_from_update = lambda update: f"chat-{update.effective_chat.id}"
    bot._log_user_message = Mock()
    bot._log_bot_message = Mock()
    bot._get_pending_multi_plan = Mock(return_value=None)
    bot._agent_mode_by_session = {"chat-1": "codex", "chat-2": "codex"}
    bot._llm_backoff_active = Mock(return_value=True)
    bot._send_response = AsyncMock()
    bot._build_single_delegation_memory_entry = Mock(return_value="receipt")
    bot.memory = SimpleNamespace(ingest=Mock())

    active_by_chat: dict[str, int] = {}
    max_active_by_chat: dict[str, int] = {}
    max_active_total = 0
    started_by_chat: dict[str, list[str]] = {}

    async def run_local_agent_task(*, session_id: str, task: str, **_kwargs):
        nonlocal max_active_total
        active_by_chat[session_id] = active_by_chat.get(session_id, 0) + 1
        max_active_by_chat[session_id] = max(
            max_active_by_chat.get(session_id, 0), active_by_chat[session_id]
        )
        max_active_total = max(max_active_total, sum(active_by_chat.values()))
        started_by_chat.setdefault(session_id, []).append(task)
        await asyncio.sleep(0)
        active_by_chat[session_id] -= 1
        return f"completed: {task}"

    bot._run_local_agent_task = run_local_agent_task
    context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))

    def make_update(chat_id: int):
        placeholder = SimpleNamespace(edit_text=AsyncMock())
        return SimpleNamespace(
            effective_chat=SimpleNamespace(id=chat_id),
            message=SimpleNamespace(reply_text=AsyncMock(return_value=placeholder)),
        )

    await asyncio.gather(
        bot._process_user_message(make_update(1), context, "first"),
        bot._process_user_message(make_update(1), context, "second"),
        bot._process_user_message(make_update(2), context, "other chat"),
    )

    assert max_active_by_chat == {"chat-1": 1, "chat-2": 1}
    assert max_active_total == 2
    assert started_by_chat["chat-1"] == ["first", "second"]
