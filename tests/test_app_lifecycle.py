from __future__ import annotations

import asyncio
import logging
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import core.app as app
from config import Config
from core.bot import LightClawBot


def test_telegram_startup_reaches_polling_with_current_memory_stats(tmp_path, monkeypatch, caplog):
    config = Config(
        telegram_bot_token="123:fixture", telegram_allowed_users=["123"],
        llm_provider="fixture", workspace_path=str(tmp_path / "workspace"),
        memory_db_path=str(tmp_path / "memory.db"),
        skills_state_path=str(tmp_path / "skills.json"),
    )
    monkeypatch.setattr("core.bot.base.LLMClient", lambda _config: SimpleNamespace(close=Mock()))
    monkeypatch.setattr(app, "load_config", lambda: config)
    bots = []

    def create_bot(config):
        bot = LightClawBot(config)
        bots.append(bot)
        return bot

    monkeypatch.setattr(app, "LightClawBot", create_bot)
    builder = Mock()
    for method in ("token", "concurrent_updates", "post_init", "post_shutdown"):
        getattr(builder, method).return_value = builder
    application = Mock()
    builder.build.return_value = application
    monkeypatch.setattr(app, "Application", SimpleNamespace(builder=lambda: builder))
    caplog.set_level(logging.INFO, logger="lightclaw")

    try:
        app.main()
        application.run_polling.assert_called_once()
        assert "sqlite-fts5-lexical" in caplog.text
    finally:
        for bot in bots:
            bot.close()


@pytest.mark.parametrize("stage", ["loop", "stats", "skills", "build", "handlers", "polling"])
@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
def test_telegram_entrypoint_closes_resources_on_failure(tmp_path, monkeypatch, stage, error_type):
    config = Config(
        telegram_bot_token="123:fixture", telegram_allowed_users=["123"],
        llm_provider="fixture", workspace_path=str(tmp_path / "workspace"),
        memory_db_path=str(tmp_path / "memory.db"),
        skills_state_path=str(tmp_path / "skills.json"),
    )
    llm = SimpleNamespace(close=Mock())
    monkeypatch.setattr("core.bot.base.LLMClient", lambda _config: llm)
    monkeypatch.setattr(app, "load_config", lambda: config)
    error = error_type(f"{stage} failed")
    if stage == "loop":
        monkeypatch.setattr(app.asyncio, "new_event_loop", Mock(side_effect=error))
    bots = []

    def create_bot(config):
        bot = LightClawBot(config)
        bots.append(bot)
        if stage == "stats":
            bot.memory.stats = Mock(side_effect=error)
        if stage == "skills":
            bot.skills.list_skills = Mock(side_effect=error)
        return bot

    monkeypatch.setattr(app, "LightClawBot", create_bot)
    builder = Mock()
    for method in ("token", "concurrent_updates", "post_init", "post_shutdown"):
        getattr(builder, method).return_value = builder
    application = Mock()
    application.run_polling.side_effect = error
    builder.build.return_value = application
    if stage == "build":
        builder.build.side_effect = error
    if stage == "handlers":
        application.add_handler.side_effect = error
    monkeypatch.setattr(app, "Application", SimpleNamespace(builder=lambda: builder))

    with pytest.raises(error_type, match=f"{stage} failed"):
        app.main()

    bot = bots[0]
    try:
        llm.close.assert_called_once_with()
        for database in (bot.memory.db, bot.jobs.db):
            with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
                database.execute("SELECT 1")
    finally:
        bot.memory.db.close()
        bot.jobs.db.close()


@pytest.mark.parametrize("outcome", ["normal", "polling-failed", "framework-shutdown-failed"])
def test_entrypoint_drains_workers_before_closing_owned_loop(tmp_path, monkeypatch, outcome):
    config = Config(
        telegram_bot_token="123:fixture", telegram_allowed_users=["123"],
        llm_provider="fixture", workspace_path=str(tmp_path / "workspace"),
        memory_db_path=str(tmp_path / "memory.db"),
        skills_state_path=str(tmp_path / "skills.json"),
    )
    monkeypatch.setattr(app, "load_config", lambda: config)
    monkeypatch.setattr(app, "_install_shutdown_signal_handlers", Mock())
    events = []
    state = {}

    def close_provider():
        assert state["worker"].done()
        assert state["cron"].done()
        assert not state["loop"].is_closed()
        events.append("provider closed")

    llm = SimpleNamespace(close=Mock(side_effect=close_provider))
    monkeypatch.setattr("core.bot.base.LLMClient", lambda _config: llm)

    def create_bot(config):
        state["bot"] = LightClawBot(config)
        return state["bot"]

    monkeypatch.setattr(app, "LightClawBot", create_bot)
    builder = Mock()
    for method in ("token", "concurrent_updates", "post_init"):
        getattr(builder, method).return_value = builder
    application = SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock()),
        add_handler=Mock(), add_error_handler=Mock(), shutdown=AsyncMock(),
    )
    builder.build.return_value = application
    monkeypatch.setattr(app, "Application", SimpleNamespace(builder=lambda: builder))
    error = RuntimeError(outcome)

    def run_polling(**kwargs):
        assert kwargs["close_loop"] is False
        loop = state["loop"] = asyncio.get_event_loop()
        loop.run_until_complete(builder.post_init.call_args.args[0](application))
        state["cron"] = state["bot"]._cron_task

        async def start_worker():
            started = asyncio.Event()

            async def worker():
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    events.append("worker stopped")

            task = state["worker"] = asyncio.create_task(worker())
            state["bot"]._active_run_tasks_by_session["123"] = task
            await started.wait()

        loop.run_until_complete(start_worker())
        if outcome == "polling-failed":
            raise error
        if outcome == "framework-shutdown-failed":
            application.shutdown.side_effect = error
        loop.run_until_complete(application.shutdown())

    application.run_polling = run_polling

    if outcome == "normal":
        app.main()
    else:
        with pytest.raises(RuntimeError, match=outcome):
            app.main()

    assert events == ["worker stopped", "provider closed"]
    assert state["loop"].is_closed()
    assert not asyncio.all_tasks(state["loop"])
    llm.close.assert_called_once_with()
