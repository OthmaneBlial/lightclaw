from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest

from core.bot import LightClawBot


@pytest.mark.asyncio
async def test_shutdown_stops_background_tasks_before_closing_resources():
    started = asyncio.Event()
    started_count = 0
    events: list[str] = []

    async def wait_forever(name: str):
        nonlocal started_count
        started_count += 1
        if started_count == 2:
            started.set()
        try:
            await asyncio.Future()
        finally:
            events.append(name)

    bot = LightClawBot.__new__(LightClawBot)
    bot._heartbeat_enabled = True
    bot._heartbeat_task = None
    bot._cron_task = None
    bot._background_tasks = set()
    heartbeat_task = bot._create_background_task(wait_forever("heartbeat"))
    cron_task = bot._create_background_task(wait_forever("cron"))
    bot._heartbeat_task = heartbeat_task
    bot._cron_task = cron_task
    bot.close = Mock(side_effect=lambda: events.append("closed"))

    await started.wait()
    await bot.shutdown()

    assert heartbeat_task.done()
    assert cron_task.done()
    assert set(events[:-1]) == {"heartbeat", "cron"}
    assert events[-1] == "closed"
    assert bot._heartbeat_task is None
    assert bot._cron_task is None
    assert not bot._background_tasks
