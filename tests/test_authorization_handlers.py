from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config import Config
from core.bot import LightClawBot

HANDLERS = (
    "cmd_start",
    "cmd_help",
    "cmd_clear",
    "cmd_wipe_memory",
    "cmd_memory",
    "cmd_recall",
    "cmd_mode",
    "cmd_skills",
    "cmd_agent",
    "cmd_cron",
    "cmd_heartbeat",
    "cmd_show",
    "handle_voice",
    "handle_photo",
    "handle_document",
    "handle_message",
)


@pytest.mark.parametrize("handler_name", HANDLERS)
async def test_unauthorized_user_cannot_reach_any_telegram_handler(handler_name: str):
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = Config()
    message = SimpleNamespace(
        text="hello",
        voice=SimpleNamespace(),
        photo=[SimpleNamespace()],
        document=SimpleNamespace(file_name="private.txt"),
        caption="",
        reply_text=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=987654),
        effective_chat=SimpleNamespace(id=123456),
        message=message,
    )
    context = SimpleNamespace(args=[], bot=SimpleNamespace())

    result = await getattr(bot, handler_name)(update, context)

    assert result is None
    message.reply_text.assert_not_awaited()
