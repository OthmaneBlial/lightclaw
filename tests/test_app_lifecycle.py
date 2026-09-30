from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import Mock

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
