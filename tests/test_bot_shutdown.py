from __future__ import annotations

import asyncio
import signal
import sqlite3
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from config import Config
from core.app import _install_shutdown_signal_handlers
from core.bot import LightClawBot


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["chat", "heartbeat"])
async def test_shutdown_waits_for_registered_requests_before_closing_storage(tmp_path, monkeypatch, kind):
    events = []
    llm = SimpleNamespace(close=Mock(side_effect=lambda: events.append("closed")))
    monkeypatch.setattr("core.bot.base.LLMClient", lambda _config: llm)
    bot = LightClawBot(Config(
        workspace_path=str(tmp_path / "workspace"), memory_db_path=str(tmp_path / "memory.db"),
        skills_state_path=str(tmp_path / "skills.json"),
    ))
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release = asyncio.Event()

    async def request():
        async with bot._memory_request_guard("123"):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cleanup_started.set()
                await release.wait()
                bot.memory.db.execute("SELECT 1")
                events.append("request drained")

    task = asyncio.create_task(request())
    if kind == "heartbeat":
        bot._background_tasks.add(task)
        bot._heartbeat_task = task
    await started.wait()
    shutdown = asyncio.create_task(bot.shutdown())
    try:
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        assert not shutdown.done()
        llm.close.assert_not_called()
        release.set()
        await asyncio.wait_for(shutdown, timeout=1)
        assert task.cancelled()
        assert not bot._active_message_clear_events_by_session
        assert events == ["request drained", "closed"]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, shutdown, return_exceptions=True)
        bot.memory.db.close()
        bot.jobs.close()


@pytest.mark.asyncio
async def test_shutdown_prevents_new_memory_request_admission():
    bot = LightClawBot.__new__(LightClawBot)
    bot._shutting_down = True
    bot._active_message_clear_events_by_session = {}

    with pytest.raises(asyncio.CancelledError):
        async with bot._memory_request_guard("123"):
            pytest.fail("Request was admitted during shutdown")

    assert not bot._active_message_clear_events_by_session


@pytest.mark.asyncio
async def test_shutdown_waits_for_confirmed_memory_wipe(tmp_path, monkeypatch):
    llm = SimpleNamespace(close=Mock())
    monkeypatch.setattr("core.bot.base.LLMClient", lambda _config: llm)
    bot = LightClawBot(Config(
        telegram_allowed_users=["123"], workspace_path=str(tmp_path / "workspace"),
        memory_db_path=str(tmp_path / "memory.db"), skills_state_path=str(tmp_path / "skills.json"),
    ))
    bot._pending_wipe_confirm["123"] = {
        "user_id": 123, "expires_at": time.time() + 90,
        "expires_monotonic": time.monotonic() + 90,
    }
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    clear_all = bot.memory.clear_all

    def blocked_clear():
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=5)
        clear_all()

    monkeypatch.setattr(bot.memory, "clear_all", blocked_clear)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123), effective_chat=SimpleNamespace(id=123, type="private"),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    wipe = asyncio.create_task(bot.cmd_wipe_memory(update, SimpleNamespace(args=["confirm"])))
    shutdown = None
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        shutdown = asyncio.create_task(bot.shutdown())
        await asyncio.sleep(0)
        assert not shutdown.done()
        llm.close.assert_not_called()
        release.set()
        await asyncio.wait_for(asyncio.gather(wipe, shutdown), timeout=1)
        assert "All memory wiped" in update.message.reply_text.await_args.args[0]
        llm.close.assert_called_once_with()
    finally:
        release.set()
        await asyncio.gather(wipe, *([shutdown] if shutdown else []), return_exceptions=True)
        bot.memory.db.close()
        bot.jobs.close()


@pytest.mark.parametrize("stage", ["jobs", "recovery", "provider", "skills", "personality", "policy"])
@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
def test_failed_bot_initialization_closes_opened_resources(tmp_path, monkeypatch, stage, error_type):
    bot = LightClawBot.__new__(LightClawBot)
    config = Config(
        workspace_path=str(tmp_path / "workspace"),
        memory_db_path=str(tmp_path / "memory.db"),
        skills_state_path=str(tmp_path / "skills.json"),
    )
    llm = SimpleNamespace(close=Mock())
    monkeypatch.setattr("core.bot.base.LLMClient", lambda _config: llm)
    error = error_type(f"{stage} initialization failed")
    target = {
        "jobs": "core.bot.base.JobStore",
        "recovery": "core.bot.base.JobStore.recover_stalled",
        "provider": "core.bot.base.LLMClient",
        "skills": "core.bot.base.SkillManager",
        "personality": "core.bot.base.load_personality",
        "policy": "core.bot.base.BotBaseMixin._compile_delegation_deny_patterns",
    }[stage]
    monkeypatch.setattr(target, Mock(side_effect=error))

    with pytest.raises(error_type, match=f"{stage} initialization failed"):
        bot.__init__(config)

    databases = [getattr(bot, name).db for name in ("memory", "jobs") if hasattr(bot, name)]
    try:
        for database in databases:
            with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
                database.execute("SELECT 1")
        if hasattr(bot, "llm"):
            llm.close.assert_called_once_with()
    finally:
        for database in databases:
            database.close()


def test_cancel_task_once_supports_python_310_task_api():
    class LegacyTask:
        def __init__(self):
            self.cancel_calls = 0

        @staticmethod
        def done():
            return False

        def cancel(self):
            self.cancel_calls += 1

    bot = LightClawBot.__new__(LightClawBot)
    task = LegacyTask()

    bot._cancel_task_once(task)
    bot._cancel_task_once(task)

    assert task.cancel_calls == 1


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
    cancellation_requested = asyncio.Event()
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
            assert cancellation_requested.is_set()
            events.append("application stop requested")

    bot = LightClawBot.__new__(LightClawBot)
    bot._shutting_down = False
    bot._background_tasks = set()
    bot._heartbeat_task = None
    bot._cron_task = None
    task = asyncio.create_task(active_run())
    bot._active_run_tasks_by_session = {"chat": task}
    bot.close = Mock()
    cancel_task_once = bot._cancel_task_once

    def record_cancellation(candidate):
        cancel_task_once(candidate)
        cancellation_requested.set()

    bot._cancel_task_once = record_cancellation
    loop = FakeLoop()
    request_stop = _install_shutdown_signal_handlers(FakeApplication(), bot, loop=loop)
    await started.wait()

    request_stop()
    assert bot._shutting_down is True
    await cleanup_started.wait()
    shutdown = asyncio.create_task(bot.shutdown())
    await asyncio.sleep(0)
    assert not task.done()
    finish_cleanup.set()
    await shutdown

    assert task.cancelled()
    assert events == ["application stop requested", "agent canceled"]
    bot.close.assert_called_once_with()
    assert set(loop.handlers) == {signal.SIGINT, signal.SIGTERM, signal.SIGABRT}
    assert request_stop() is None
