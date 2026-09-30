from __future__ import annotations

import asyncio
import threading
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


@pytest.mark.asyncio
@pytest.mark.parametrize("clear_kind", ["session", "global"])
async def test_clear_drops_inflight_and_queued_chat_history(monkeypatch, clear_kind):
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(
        memory_top_k=3,
        context_window=128,
        llm_provider="fixture",
        workspace_path="/tmp",
    )
    bot.personality = None
    bot.is_update_allowed = lambda _update: True
    bot._session_id_from_update = lambda _update: "chat-42"
    bot._log_user_message = Mock()
    bot._log_bot_message = Mock()
    bot._get_pending_multi_plan = Mock(return_value=None)
    bot._agent_mode_by_session = {}
    bot._llm_backoff_active = Mock(return_value=False)
    bot._clear_llm_backoff = Mock()
    bot._is_provider_error_text = Mock(return_value=False)
    bot._is_context_error = Mock(return_value=False)
    bot._filter_recalled_memories = lambda items: items
    bot._clean_orphan_messages = lambda items: items
    bot._filter_recent_context = lambda items: items
    bot._get_session_summary = Mock(return_value="")
    bot._get_file_mode = Mock(return_value="chat")
    bot._is_file_intent = Mock(return_value=False)
    bot._process_file_blocks = AsyncMock(return_value=([], "answer"))
    bot._strip_fenced_code_for_chat = lambda text: text
    bot._is_large_code_leak = Mock(return_value=False)
    bot._send_response = AsyncMock()
    bot._create_background_task = Mock()
    bot._privileged_rate_limited = Mock(return_value=False)
    bot._clear_pending_actions = Mock()
    bot._invalidate_active_summaries = Mock()
    bot._invalidate_session_summary = Mock()
    bot._pending_wipe_confirm = {
        "chat-42": {
            "user_id": 42,
            "expires_at": 9_999_999_999,
            "expires_monotonic": 9_999_999_999,
        }
    }
    bot._summary_key = lambda _session: ("chat-42", "user", "workspace")
    bot._session_summaries = {}
    bot._reply_logged = AsyncMock()

    started = asyncio.Event()
    release = asyncio.Event()

    async def chat(*_args, **_kwargs):
        nonlocal chat_calls
        chat_calls += 1
        started.set()
        await release.wait()
        return "answer"

    chat_calls = 0
    bot.llm = SimpleNamespace(chat=chat)
    bot.skills = SimpleNamespace(prompt_context=lambda _session: "")
    bot.memory = SimpleNamespace(
        recall=Mock(return_value=[]),
        format_memories_for_prompt=Mock(return_value=""),
        get_recent=Mock(return_value=[]),
        ingest=Mock(),
        clear_session=Mock(),
        clear_all=Mock(),
    )
    monkeypatch.setattr("core.bot.handlers.build_system_prompt", lambda *_args: "prompt")

    placeholder = SimpleNamespace(edit_text=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=42, type="private"),
        message=SimpleNamespace(reply_text=AsyncMock(return_value=placeholder)),
    )
    context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))
    processing = asyncio.create_task(
        bot._process_user_message(update, context, "remember this")
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    queued_update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=42, type="private"),
        message=SimpleNamespace(reply_text=AsyncMock(return_value=placeholder)),
    )
    queued = asyncio.create_task(
        bot._process_user_message(queued_update, context, "queued request")
    )
    await asyncio.sleep(0)
    assert len(bot._active_message_clear_events_by_session["chat-42"]) == 2
    if clear_kind == "session":
        await bot.cmd_clear(update, SimpleNamespace())
    else:
        await bot.cmd_wipe_memory(update, SimpleNamespace(args=["confirm"]))
    release.set()
    await asyncio.gather(processing, queued)

    if clear_kind == "session":
        bot.memory.clear_session.assert_called_once_with("chat-42")
    else:
        bot.memory.clear_all.assert_called_once_with()
    bot.memory.ingest.assert_not_called()
    assert chat_calls == 1
    assert bot._active_message_clear_events_by_session == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 2, 3])
async def test_cancelled_global_wipe_finishes_before_new_chat_is_admitted(cancel_count):
    bot = LightClawBot.__new__(LightClawBot)
    bot.is_update_allowed = lambda _update: True
    bot._session_id_from_update = lambda _update: "chat-42"
    bot._log_user_message = Mock()
    bot._clear_pending_actions = Mock()
    bot._invalidate_active_summaries = Mock()
    bot._pending_wipe_confirm = {
        "chat-42": {
            "user_id": 42,
            "expires_at": 9_999_999_999,
            "expires_monotonic": 9_999_999_999,
        }
    }
    bot._session_summaries = {"chat-42": "old summary"}
    bot._reply_logged = AsyncMock()
    history = ["old message"]
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def clear_all():
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=5), "wipe was not released"
        history.clear()

    async def process(_update, _context, user_text, _clear_event):
        history.append(user_text)

    bot.memory = SimpleNamespace(clear_all=clear_all)
    bot._process_user_message_serialized = process
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        message=SimpleNamespace(),
    )
    wipe = asyncio.create_task(
        bot.cmd_wipe_memory(update, SimpleNamespace(args=["confirm"]))
    )
    new_chat = None
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        for _ in range(cancel_count):
            wipe.cancel()
            await asyncio.sleep(0)
            assert bot._get_memory_wipe_lock().locked()
            assert not wipe.done()
        new_chat = asyncio.create_task(
            bot._process_user_message(update, SimpleNamespace(), "new message")
        )
        await asyncio.sleep(0)
        assert not new_chat.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await wipe
        await asyncio.wait_for(new_chat, timeout=2)
        assert history == ["new message"]
        assert bot._session_summaries == {}
        bot._reply_logged.assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(wipe, *([new_chat] if new_chat else []), return_exceptions=True)
