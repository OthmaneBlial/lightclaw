from __future__ import annotations

import asyncio

import pytest

from core.bot import LightClawBot


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
