from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from config import Config
from core.bot import LightClawBot
from memory import MemoryStore


@pytest.mark.asyncio
async def test_nested_memory_guards_share_invalidation_and_keep_outer_registration():
    bot = LightClawBot.__new__(LightClawBot)
    current = asyncio.current_task()
    async with bot._memory_request_guard("chat") as outer:
        async with bot._memory_request_guard("chat") as inner:
            assert inner is outer
            bot._invalidate_active_message_requests("chat")
            assert outer.is_set()
        assert bot._active_message_clear_events_by_session["chat"][current] is outer
    assert not bot._active_message_clear_events_by_session


@pytest.mark.asyncio
async def test_memory_ingestion_runs_off_loop_and_skips_after_clear():
    loop_thread = threading.get_ident()
    worker_threads = []
    clear_event = asyncio.Event()

    def ingest(*_args):
        worker_threads.append(threading.get_ident())
        return 1

    bot = LightClawBot.__new__(LightClawBot)
    bot.memory = SimpleNamespace(ingest=Mock(side_effect=ingest))

    assert await bot._ingest_memory("user", "goal", "chat", clear_event=clear_event) == 1
    assert len(worker_threads) == 1
    assert worker_threads[0] != loop_thread

    pending_clear_event = asyncio.Event()
    lock = bot._get_memory_write_lock()
    await lock.acquire()
    try:
        pending_write = asyncio.create_task(
            bot._ingest_memory("assistant", "answer", "chat", clear_event=pending_clear_event)
        )
        await asyncio.sleep(0)
        pending_clear_event.set()
    finally:
        lock.release()

    assert await pending_write is None
    bot.memory.ingest.assert_called_once_with("user", "goal", "chat")


@pytest.mark.asyncio
async def test_session_scope_binding_runs_off_event_loop(tmp_path):
    loop_thread = threading.get_ident()
    binding = {}

    def bind_session(session_id, *, user_namespace, workspace_namespace):
        binding.update(
            thread=threading.get_ident(),
            session_id=session_id,
            user_namespace=user_namespace,
            workspace_namespace=workspace_namespace,
        )

    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(workspace_path=str(tmp_path))
    bot.memory = SimpleNamespace(bind_session=bind_session)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=456),
    )

    assert await bot._session_scope_from_update(update) == "456"
    assert binding["thread"] != loop_thread
    assert binding["session_id"] == "456"
    assert binding["user_namespace"] == "telegram-user:42"
    assert binding["workspace_namespace"] == str(tmp_path.resolve())


@pytest.mark.asyncio
async def test_concurrent_group_updates_keep_memory_scopes_per_user(tmp_path):
    store = MemoryStore(str(tmp_path / "memory.db"))
    workspace = str(tmp_path.resolve())
    session_id = "-100"
    store.ingest(
        "user", "private marker phrase cobalt", session_id,
        user_namespace="telegram-user:1", workspace_namespace=workspace,
    )
    store.ingest(
        "user", "private marker phrase amber", session_id,
        user_namespace="telegram-user:2", workspace_namespace=workspace,
    )

    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(
        workspace_path=workspace,
        telegram_allowed_users=[],
        telegram_public_bot_ack=True,
    )
    bot.memory = store

    def update(user_id):
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id),
            effective_chat=SimpleNamespace(id=-100, type="group"),
        )

    first_bound = asyncio.Event()
    second_bound = asyncio.Event()

    async def first_user():
        session = await bot._session_scope_from_update(update(1))
        first_bound.set()
        await second_bound.wait()
        return await asyncio.to_thread(store.recall, "private marker phrase", session_id=session)

    async def second_user():
        await first_bound.wait()
        session = await bot._session_scope_from_update(update(2))
        second_bound.set()
        return await asyncio.to_thread(store.recall, "private marker phrase", session_id=session)

    try:
        first, second = await asyncio.gather(first_user(), second_user())
        assert [record.content for record in first] == ["private marker phrase cobalt"]
        assert [record.content for record in second] == ["private marker phrase amber"]
    finally:
        store.db.close()


@pytest.mark.asyncio
async def test_clear_during_initial_memory_write_prevents_agent_start():
    loop = asyncio.get_running_loop()
    write_started = asyncio.Event()
    release_write = threading.Event()

    def ingest(*_args):
        loop.call_soon_threadsafe(write_started.set)
        assert release_write.wait(timeout=2)
        return 1

    bot = LightClawBot.__new__(LightClawBot)
    bot._session_id_from_update = lambda _update: "chat-1"
    bot._log_user_message = Mock()
    bot._log_bot_message = Mock()
    bot._get_pending_multi_plan = Mock(return_value=None)
    bot._agent_mode_by_session = {"chat-1": "codex"}
    bot._send_response = AsyncMock()
    bot._run_local_agent_task = AsyncMock(return_value="should not run")
    bot.memory = SimpleNamespace(ingest=Mock(side_effect=ingest))
    context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))
    placeholder = SimpleNamespace(edit_text=AsyncMock())
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=1),
        message=SimpleNamespace(reply_text=AsyncMock(return_value=placeholder)),
    )

    task = asyncio.create_task(bot._process_user_message(update, context, "goal"))
    try:
        await asyncio.wait_for(write_started.wait(), timeout=2)
        bot._invalidate_active_message_requests("chat-1")
        release_write.set()
        await task
    finally:
        release_write.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    bot._run_local_agent_task.assert_not_awaited()
    bot._send_response.assert_awaited_once_with(
        placeholder, update, "🗑️ Request cleared before agent execution."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["direct", "run", "observe"])
@pytest.mark.parametrize("clear_kind", ["session", "global"])
async def test_agent_commands_do_not_restore_cleared_memory(tmp_path, route, clear_kind):
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = Config(telegram_allowed_users=["42"], workspace_path=str(tmp_path))
    bot.memory = MemoryStore(str(tmp_path / "memory.db"))
    bot._privileged_rate_limited = Mock(return_value=False)
    bot._session_summaries = {}
    bot._summary_generation_by_session = {}
    bot._summarizing = set()
    bot._pending_wipe_confirm = {}
    for name in (
        "_pending_multi_plan_by_session", "_pending_trusted_agent_run_by_session",
        "_pending_voice_goal_by_session", "_voice_request_ids_by_session",
    ):
        setattr(bot, name, {})
    bot._reply_logged = AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock()))
    bot._send_response = AsyncMock()
    bot._llm_backoff_active = Mock(return_value=False)
    bot.maybe_summarize = Mock()
    bot._create_background_task = Mock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=456, type="private"),
        message=SimpleNamespace(),
    )
    await bot._session_scope_from_update(update)
    bot.memory.ingest("user", "old private goal", "456")
    bot.memory.ingest("user", "other chat history", "other-chat")
    if clear_kind == "global":
        await bot.cmd_wipe_memory(update, SimpleNamespace(args=[]))

    async def clear_during_run(*_args, **_kwargs):
        if clear_kind == "session":
            await bot.cmd_clear(update, SimpleNamespace())
        else:
            await bot.cmd_wipe_memory(update, SimpleNamespace(args=["confirm"]))
        return "✅ Finished in 0.1s."

    bot._run_local_agent_task = clear_during_run
    args = {
        "direct": ["codex", "old private goal"],
        "run": ["run", "codex", "old private goal"],
        "observe": ["observe", "codex", "old private goal"],
    }[route]
    try:
        await bot.cmd_agent(update, SimpleNamespace(args=args))
        assert bot.memory.get_recent(session_id="456") == []
        assert len(bot.memory.get_recent(session_id="other-chat")) == (clear_kind == "session")
        bot._send_response.assert_awaited_once()
        bot._create_background_task.assert_not_called()
        assert not getattr(bot, "_active_message_clear_events_by_session", {})
    finally:
        bot.memory.db.close()


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
    bot._heartbeat_last_chat_id = "chat-1"

    active_by_chat: dict[str, int] = {}
    max_active_by_chat: dict[str, int] = {}
    max_active_total = 0
    started_by_chat: dict[str, list[str]] = {}
    other_chat_started = asyncio.Event()

    async def run_local_agent_task(*, session_id: str, task: str, **_kwargs):
        nonlocal max_active_total
        assert bot._heartbeat_last_chat_id == "chat-1"
        active_by_chat[session_id] = active_by_chat.get(session_id, 0) + 1
        max_active_by_chat[session_id] = max(
            max_active_by_chat.get(session_id, 0), active_by_chat[session_id]
        )
        max_active_total = max(max_active_total, sum(active_by_chat.values()))
        started_by_chat.setdefault(session_id, []).append(task)
        if session_id == "chat-1":
            await other_chat_started.wait()
        else:
            other_chat_started.set()
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
    assert bot._heartbeat_last_chat_id == "chat-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("clear_kind", ["session", "global"])
@pytest.mark.parametrize(
    ("chat_type", "session_only"), [("private", False), ("group", True)]
)
async def test_clear_drops_inflight_and_queued_chat_history(
    monkeypatch, clear_kind, chat_type, session_only
):
    bot = LightClawBot.__new__(LightClawBot)
    loop_thread = threading.get_ident()
    memory_read_threads = []

    def record_memory_read(result):
        memory_read_threads.append(threading.get_ident())
        return result

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
    bot._get_session_summary = AsyncMock(return_value="")
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
        recall=Mock(side_effect=lambda *_args, **_kwargs: record_memory_read([])),
        format_memories_for_prompt=Mock(return_value=""),
        get_recent=Mock(side_effect=lambda *_args, **_kwargs: record_memory_read([])),
        ingest=Mock(),
        clear_session=Mock(),
        clear_all=Mock(),
    )
    monkeypatch.setattr("core.bot.handlers.build_system_prompt", lambda *_args: "prompt")

    placeholder = SimpleNamespace(edit_text=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=42, type=chat_type),
        message=SimpleNamespace(reply_text=AsyncMock(return_value=placeholder)),
    )
    context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))
    processing = asyncio.create_task(
        bot._process_user_message(update, context, "remember this")
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    queued_update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=42, type=chat_type),
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
    assert len(memory_read_threads) == 2
    assert all(thread_id != loop_thread for thread_id in memory_read_threads)
    assert all(
        call.kwargs["current_session_only"] is session_only
        for call in bot.memory.recall.call_args_list
    )
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
