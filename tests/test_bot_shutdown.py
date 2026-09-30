from __future__ import annotations

import asyncio
import signal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.app import _install_shutdown_signal_handlers
from core.bot import LightClawBot


def test_close_releases_all_resources_when_provider_close_fails():
    bot = LightClawBot.__new__(LightClawBot)
    bot.llm = Mock()
    bot.llm.close.side_effect = RuntimeError("provider close failed")
    bot.jobs = Mock()
    bot.memory = SimpleNamespace(db=Mock())

    with pytest.raises(RuntimeError, match="provider close failed"):
        bot.close()

    bot.jobs.close.assert_called_once_with()
    bot.memory.db.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_shutdown_stops_background_tasks_before_closing_resources():
    started = asyncio.Event()
    started_count = 0
    events: list[str] = []

    async def wait_forever(name: str):
        nonlocal started_count
        started_count += 1
        if started_count == 3:
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
    active_run_task = asyncio.create_task(wait_forever("agent run"))
    bot._active_run_tasks_by_session = {"chat": active_run_task}
    bot._shutting_down = False
    bot._heartbeat_task = heartbeat_task
    bot._cron_task = cron_task
    bot.close = Mock(side_effect=lambda: events.append("closed"))

    await started.wait()
    await bot.shutdown()

    assert heartbeat_task.done()
    assert cron_task.done()
    assert active_run_task.done()
    assert set(events[:-1]) == {"heartbeat", "cron", "agent run"}
    assert events[-1] == "closed"
    assert bot._heartbeat_task is None
    assert bot._cron_task is None
    assert not bot._background_tasks


@pytest.mark.asyncio
async def test_shutdown_signal_cancels_agents_before_stopping_application():
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    finish_cleanup = asyncio.Event()
    events: list[str] = []

    async def active_run():
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cleanup_started.set()
            await finish_cleanup.wait()
            events.append("agent canceled")
            raise

    class FakeLoop:
        def __init__(self):
            self.handlers = {}

        def add_signal_handler(self, stop_signal, callback):
            self.handlers[stop_signal] = callback

        @staticmethod
        def create_task(coroutine):
            return asyncio.create_task(coroutine)

    class FakeApplication:
        def stop_running(self):
            assert task.cancelling()
            events.append("application stop requested")

    bot = LightClawBot.__new__(LightClawBot)
    bot._shutting_down = False
    bot._background_tasks = set()
    bot._heartbeat_task = None
    bot._cron_task = None
    task = asyncio.create_task(active_run())
    bot._active_run_tasks_by_session = {"chat": task}
    bot.close = Mock()
    loop = FakeLoop()
    request_stop = _install_shutdown_signal_handlers(FakeApplication(), bot, loop=loop)
    await started.wait()

    request_stop()
    assert bot._shutting_down is True
    await cleanup_started.wait()
    shutdown = asyncio.create_task(bot.shutdown())
    await asyncio.sleep(0)
    assert task.cancelling() == 1
    finish_cleanup.set()
    await shutdown

    assert task.cancelled()
    assert events == ["application stop requested", "agent canceled"]
    bot.close.assert_called_once_with()
    assert set(loop.handlers) == {signal.SIGINT, signal.SIGTERM, signal.SIGABRT}
    assert request_stop() is None
