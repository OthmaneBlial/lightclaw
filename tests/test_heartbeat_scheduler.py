from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram.error import NetworkError

from core.bot import LightClawBot


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
