from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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
    "handle_run_action",
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
        effective_chat=SimpleNamespace(id=123456, type="private"),
        message=message,
        callback_query=SimpleNamespace(
            data="lc:run:cancel",
            answer=AsyncMock(),
            message=message,
        ),
    )
    context = SimpleNamespace(args=[], bot=SimpleNamespace())

    result = await getattr(bot, handler_name)(update, context)

    assert result is None
    message.reply_text.assert_not_awaited()


@pytest.mark.parametrize("handler_name", HANDLERS)
async def test_allowed_user_cannot_use_authorized_bot_from_group_chat(handler_name: str):
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = Config(telegram_allowed_users=["987654"])
    message = SimpleNamespace(
        text="hello",
        voice=SimpleNamespace(),
        photo=[SimpleNamespace()],
        document=SimpleNamespace(file_name="private.txt"),
        caption="",
        reply_text=AsyncMock(),
    )
    query = SimpleNamespace(data="lc:run:cancel", answer=AsyncMock(), message=message)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=987654),
        effective_chat=SimpleNamespace(id=-123456, type="group"),
        message=message,
        callback_query=query,
    )
    context = SimpleNamespace(args=[], bot=SimpleNamespace())

    result = await getattr(bot, handler_name)(update, context)

    assert result is None
    message.reply_text.assert_not_awaited()
    if handler_name == "handle_run_action":
        query.answer.assert_awaited_once_with("Not authorized", show_alert=True)
    else:
        query.answer.assert_not_awaited()


@pytest.mark.parametrize(
    ("handler_name", "attachment", "notice"),
    [
        ("handle_photo", "photo", "can't inspect photo"),
        ("handle_document", "document", "can't read Telegram file"),
    ],
)
async def test_unsupported_attachments_are_not_sent_to_the_model(
    handler_name, attachment, notice
):
    bot = LightClawBot.__new__(LightClawBot)
    bot.is_update_allowed = lambda _update: True
    bot._reply_logged = AsyncMock()
    bot._process_user_message = AsyncMock()
    message = SimpleNamespace(
        photo=[SimpleNamespace()] if attachment == "photo" else [],
        document=SimpleNamespace(file_name="report.pdf") if attachment == "document" else None,
        caption="summarize this",
    )
    update = SimpleNamespace(effective_user=SimpleNamespace(id=123), message=message)

    await getattr(bot, handler_name)(update, SimpleNamespace())

    bot._reply_logged.assert_awaited_once()
    assert notice in bot._reply_logged.await_args.args[1]
    bot._process_user_message.assert_not_awaited()


async def test_text_rate_limit_rejects_before_model_processing():
    bot = LightClawBot.__new__(LightClawBot)
    bot.is_update_allowed = lambda _update: True
    bot._privileged_rate_limited = Mock(return_value=True)
    bot._reply_logged = AsyncMock()
    bot._process_user_message = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        message=SimpleNamespace(text="hello"),
    )

    await bot.handle_message(update, SimpleNamespace())

    bot._privileged_rate_limited.assert_called_once_with(123, "message", limit=20)
    assert "Too many text messages" in bot._reply_logged.await_args.args[1]
    bot._process_user_message.assert_not_awaited()
