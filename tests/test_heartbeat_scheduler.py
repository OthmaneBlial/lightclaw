from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram.error import NetworkError

from config import Config
from core.bot import LightClawBot
from memory import MemoryStore


@pytest.mark.asyncio
async def test_overflowing_heartbeat_interval_preserves_existing_schedule(tmp_path):
    bot = LightClawBot.__new__(LightClawBot)
    bot.is_update_allowed = lambda _update: True
    bot._privileged_rate_limited = lambda *_args, **_kwargs: False
    bot._session_id_from_update = lambda _update: "99"
    bot._log_user_message = Mock()
    bot._reply_logged = AsyncMock()
    bot._heartbeat_enabled = True
    bot._heartbeat_interval_sec = 300
    bot._heartbeat_last_chat_id = "42"
    task = SimpleNamespace(done=lambda: False, cancel=Mock())
    bot._heartbeat_task = task
    bot._ensure_heartbeat_task = AsyncMock()
    bot._heartbeat_file_path = lambda: tmp_path / "HEARTBEAT.md"
    update = SimpleNamespace(effective_user=SimpleNamespace(id=99), message=object())
    context = SimpleNamespace(
        args=["on", "9" * 400], bot=SimpleNamespace(send_message=AsyncMock())
    )

    await bot.cmd_heartbeat(update, context)

    assert bot._heartbeat_enabled
    assert bot._heartbeat_interval_sec == 300
    assert bot._heartbeat_last_chat_id == "42"
    assert bot._heartbeat_task is task
    task.cancel.assert_not_called()
    bot._ensure_heartbeat_task.assert_not_awaited()
    assert "too large" in bot._reply_logged.await_args.args[1].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_ids", [(42, 99), (-7, -7)])
async def test_heartbeat_target_and_scope_change_only_on_explicit_enable(
    tmp_path, monkeypatch, chat_ids
):
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(workspace_path=str(tmp_path))
    bot.memory = MemoryStore(tmp_path / "memory.db")
    bot.is_update_allowed = lambda _update: True
    bot._privileged_rate_limited = lambda *_args, **_kwargs: False
    bot._log_user_message = Mock()
    bot._reply_logged = AsyncMock()
    bot._heartbeat_enabled = False
    bot._heartbeat_interval_sec = 300
    bot._heartbeat_last_chat_id = ""
    bot._heartbeat_last_run_at = 0
    bot._heartbeat_task = None
    bot._background_tasks = set()
    bot._heartbeat_file_path = lambda: tmp_path / "HEARTBEAT.md"
    ticks = asyncio.Queue()
    runs = asyncio.Queue()

    async def wait_for_tick(_seconds):
        await ticks.get()

    async def record_run(_bot, session_id):
        runs.put_nowait((session_id, bot.memory.scope_for(session_id)))

    monkeypatch.setattr("core.bot.commands.heartbeat.asyncio.sleep", wait_for_tick)
    bot._run_heartbeat_once = record_run
    context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()), args=["on"])

    def update(user_id, chat_id):
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id),
            effective_chat=SimpleNamespace(id=chat_id),
            message=SimpleNamespace(),
        )

    async def next_run():
        ticks.put_nowait(None)
        return await asyncio.wait_for(runs.get(), timeout=2)

    try:
        await bot.cmd_heartbeat(update(42, chat_ids[0]), context)
        first_task = bot._heartbeat_task
        expected_first = (
            str(chat_ids[0]), ("telegram-user:42", str(tmp_path.resolve()))
        )
        assert await next_run() == expected_first

        await bot.cmd_heartbeat(update(99, chat_ids[1]), SimpleNamespace(args=["show"]))
        assert bot._heartbeat_last_chat_id == str(chat_ids[0])
        assert await next_run() == expected_first

        await bot.cmd_heartbeat(update(99, chat_ids[1]), context)
        assert bot._heartbeat_task is not first_task
        await first_task
        assert await next_run() == (
            str(chat_ids[1]), ("telegram-user:99", str(tmp_path.resolve()))
        )
    finally:
        task = bot._heartbeat_task
        bot._heartbeat_enabled = False
        bot._stop_heartbeat_task()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        bot.memory.db.close()


@pytest.mark.asyncio
async def test_oversized_heartbeat_file_is_skipped_before_model_call(tmp_path, monkeypatch):
    heartbeat = tmp_path / "HEARTBEAT.md"
    heartbeat.write_bytes(b"x" * (64 * 1024 + 1))
    bot = LightClawBot.__new__(LightClawBot)
    bot._heartbeat_file_path = lambda: heartbeat
    bot._llm_backoff_active = lambda: False
    bot.config = SimpleNamespace(memory_top_k=4)
    bot.memory = SimpleNamespace(
        recall=Mock(return_value=[]), format_memories_for_prompt=Mock(return_value="")
    )
    bot._filter_recalled_memories = lambda memories: memories
    bot._get_session_summary = lambda _session_id: ""
    bot.skills = SimpleNamespace(prompt_context=lambda _session_id: "")
    bot.personality = object()
    bot.llm = SimpleNamespace(chat=AsyncMock(return_value="NO_UPDATE"))
    bot._is_provider_error_text = lambda _response: False
    bot._clear_llm_backoff = Mock()
    monkeypatch.setattr(
        "core.bot.commands.heartbeat.build_system_prompt", lambda *_args: ""
    )

    await bot._run_heartbeat_once(None, "123")

    bot.llm.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_heartbeat_does_not_clear_restarted_task(monkeypatch):
    old_started = asyncio.Event()
    new_started = asyncio.Event()
    sleep_calls = 0

    async def blocked_sleep(_seconds: int):
        nonlocal sleep_calls
        sleep_calls += 1
        (old_started if sleep_calls == 1 else new_started).set()
        await asyncio.Future()

    monkeypatch.setattr("core.bot.commands.heartbeat.asyncio.sleep", blocked_sleep)
    bot = LightClawBot.__new__(LightClawBot)
    bot._heartbeat_enabled = True
    bot._heartbeat_interval_sec = 300
    bot._heartbeat_last_chat_id = ""
    bot._background_tasks = set()
    bot._heartbeat_task = asyncio.create_task(bot._heartbeat_loop(None))
    old_task = bot._heartbeat_task

    await old_started.wait()
    bot._stop_heartbeat_task()
    bot._heartbeat_enabled = True
    await bot._ensure_heartbeat_task(None)
    new_task = bot._heartbeat_task
    await new_started.wait()
    await old_task

    try:
        assert bot._heartbeat_task is new_task
    finally:
        if new_task and not new_task.done():
            new_task.cancel()
            await new_task


@pytest.mark.asyncio
async def test_telegram_delivery_error_does_not_stop_heartbeat_scheduler(monkeypatch):
    bot = LightClawBot.__new__(LightClawBot)
    bot._heartbeat_enabled = True
    bot._heartbeat_interval_sec = 300
    bot._heartbeat_last_chat_id = "123"
    bot._heartbeat_task = None
    attempts = 0
    sleep_calls = 0

    async def fail_delivery(_bot, _session_id):
        nonlocal attempts
        attempts += 1
        raise NetworkError("temporary failure")

    async def stop_after_next_interval(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 2:
            bot._heartbeat_enabled = False

    bot._run_heartbeat_once = fail_delivery
    monkeypatch.setattr("core.bot.commands.heartbeat.asyncio.sleep", stop_after_next_interval)

    await bot._heartbeat_loop(None)

    assert attempts == 1
    assert sleep_calls == 2


@pytest.mark.asyncio
async def test_unexpected_heartbeat_error_does_not_stop_scheduler(monkeypatch):
    bot = LightClawBot.__new__(LightClawBot)
    bot._heartbeat_enabled = True
    bot._heartbeat_interval_sec = 300
    bot._heartbeat_last_chat_id = "123"
    bot._heartbeat_task = None
    attempts = 0
    sleep_calls = 0

    async def fail_once(_bot, _session_id):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary storage failure")

    async def stop_after_retry(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 3:
            bot._heartbeat_enabled = False

    bot._run_heartbeat_once = fail_once
    monkeypatch.setattr("core.bot.commands.heartbeat.asyncio.sleep", stop_after_retry)

    await bot._heartbeat_loop(None)

    assert attempts == 2
    assert sleep_calls == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["skills", "model", "delivery"])
@pytest.mark.parametrize("erase", ["current", "other", "global", "none"])
async def test_memory_clear_does_not_restore_inflight_heartbeat_history(
    tmp_path, monkeypatch, phase, erase
):
    heartbeat = tmp_path / "HEARTBEAT.md"
    heartbeat.write_text("Summarize the last conversation.")
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = Config(workspace_path=str(tmp_path), telegram_allowed_users=["42"])
    bot.memory = MemoryStore(tmp_path / "memory.db")
    bot.memory.bind_session(
        "123", user_namespace="telegram-user:42", workspace_namespace=str(tmp_path.resolve())
    )
    bot.memory.ingest("user", "old secret amberfalcon", "123")
    bot._heartbeat_file_path = lambda: heartbeat
    bot._llm_backoff_active = lambda: False
    bot._filter_recalled_memories = lambda memories: memories
    bot._get_session_summary = lambda _session_id: ""
    bot.personality = object()
    bot._is_provider_error_text = lambda _response: False
    bot._clear_llm_backoff = Mock()
    bot._process_file_blocks = AsyncMock(return_value=([], "old secret amberfalcon"))
    bot._repair_incomplete_html = AsyncMock(return_value=[])
    bot._workspace_display_path = lambda: "workspace"
    bot._compact_response_for_file_ops = lambda response: response
    bot._privileged_rate_limited = lambda *_args, **_kwargs: False
    bot._log_user_message = Mock()
    bot._reply_logged = AsyncMock()
    bot._pending_wipe_confirm = {}
    bot._pending_multi_plan_by_session = {}
    bot._pending_trusted_agent_run_by_session = {}
    bot._pending_voice_goal_by_session = {}
    bot._voice_request_ids_by_session = {}
    bot._session_summaries = {}
    bot._invalidate_session_summary = Mock()
    bot._invalidate_active_summaries = Mock()
    entered = asyncio.Event()
    release = asyncio.Event()
    release_skills = threading.Event()
    loop = asyncio.get_running_loop()
    calls = 0

    def skills_context(_session_id):
        if phase == "skills":
            loop.call_soon_threadsafe(entered.set)
            assert release_skills.wait(timeout=2)
        return ""

    bot.skills = SimpleNamespace(prompt_context=skills_context)

    async def model(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            return ""
        if phase == "model":
            entered.set()
            await release.wait()
        return "old secret amberfalcon"

    async def deliver(**_kwargs):
        if phase == "delivery":
            entered.set()
            await release.wait()

    bot.llm = SimpleNamespace(chat=AsyncMock(side_effect=model))
    transport = SimpleNamespace(send_message=AsyncMock(side_effect=deliver))
    monkeypatch.setattr("core.bot.commands.heartbeat.build_system_prompt", lambda *_args: "")
    run = asyncio.create_task(bot._run_heartbeat_once(transport, "123"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=456 if erase == "other" else 123, type="private"),
            message=SimpleNamespace(),
        )
        if erase == "global":
            await bot.cmd_wipe_memory(update, SimpleNamespace(args=[]))
            await bot.cmd_wipe_memory(update, SimpleNamespace(args=["confirm"]))
        elif erase != "none":
            await bot.cmd_clear(update, SimpleNamespace())
        release.set()
        release_skills.set()
        await run

        saved = bot.memory.get_recent("123")
        if erase in {"other", "none"}:
            assert any("[heartbeat]" in entry["content"] for entry in saved)
        else:
            assert saved == []
        if phase == "skills" and erase in {"current", "global"}:
            bot.llm.chat.assert_not_awaited()
            transport.send_message.assert_not_awaited()
        else:
            transport.send_message.assert_awaited_once()
        assert not getattr(bot, "_active_message_clear_events_by_session", {})
    finally:
        release.set()
        release_skills.set()
        if not run.done():
            run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        bot.memory.db.close()
