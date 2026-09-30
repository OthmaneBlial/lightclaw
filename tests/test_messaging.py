from __future__ import annotations

from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError

from core.bot.messaging import BotMessagingMixin


class MessagingHarness(BotMessagingMixin):
    @staticmethod
    def _session_id_from_update(_update) -> str:
        return "fixture-session"

    @staticmethod
    def _log_bot_message(_session_id: str, _text: str) -> None:
        pass


@pytest.mark.asyncio
async def test_escaped_content_is_not_truncated_after_html_conversion():
    bot = MessagingHarness()
    placeholder = SimpleNamespace(edit_text=AsyncMock())
    update = SimpleNamespace(message=None)
    source = "<" * 1500

    await bot._send_response(placeholder, update, source)

    html_chunk = placeholder.edit_text.await_args.args[0]
    assert len(html_chunk) > 4096
    assert html_chunk == "&lt;" * 1500


@pytest.mark.asyncio
async def test_long_fenced_code_keeps_html_formatting_across_messages():
    bot = MessagingHarness()
    placeholder = SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(reply_text=AsyncMock())
    source = (
        "Before\n```\n"
        + "value = '<tag> 😀 & text'\n" * 160
        + "```\nAfter **bold**"
    )

    await bot._send_response(placeholder, SimpleNamespace(message=message), source)

    chunks = [placeholder.edit_text.await_args.args[0]] + [
        call.args[0] for call in message.reply_text.await_args_list
    ]
    assert len(chunks) > 1

    class HTMLText(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack: list[str] = []
            self.text: list[str] = []

        def handle_starttag(self, tag, _attrs):
            self.stack.append(tag)

        def handle_endtag(self, tag):
            assert self.stack.pop() == tag

        def handle_data(self, data):
            self.text.append(data)

    parsed = []
    for chunk in chunks:
        parser = HTMLText()
        parser.feed(chunk)
        parser.close()
        assert not parser.stack
        assert len("".join(parser.text).encode("utf-16-le")) // 2 <= 3000
        parsed.extend(parser.text)

    assert "".join(parsed) == "Before\n" + "value = '<tag> 😀 & text'\n" * 160 + "\nAfter bold"


@pytest.mark.asyncio
async def test_plain_fallback_decodes_escaped_html_entities():
    bot = MessagingHarness()
    calls: list[tuple[str, str | None]] = []

    async def send(text: str, parse_mode: str | None = None):
        calls.append((text, parse_mode))
        if parse_mode:
            raise BadRequest("bad markup")

    assert await bot._try_send(send, "A &lt; B &amp; <b>bold</b>")
    assert calls == [
        ("A &lt; B &amp; <b>bold</b>", ParseMode.HTML),
        ("A < B & bold", None),
    ]


@pytest.mark.asyncio
async def test_uncertain_edit_failure_does_not_trigger_a_second_message():
    bot = MessagingHarness()
    placeholder = SimpleNamespace(edit_text=AsyncMock(side_effect=NetworkError("timed out")))
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(message=message)

    with pytest.raises(NetworkError, match="timed out"):
        await bot._send_response(placeholder, update, "final result")

    placeholder.edit_text.assert_awaited_once()
    message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_oversized_long_response_is_kept_local(tmp_path, monkeypatch):
    monkeypatch.setattr("core.bot.messaging.TELEGRAM_BOT_API_MAX_FILE_BYTES", 8)
    bot = MessagingHarness()
    bot.config = SimpleNamespace(workspace_path=str(tmp_path / "workspace"))
    message = SimpleNamespace(reply_document=AsyncMock(), reply_text=AsyncMock())

    await bot._send_response(None, SimpleNamespace(message=message), "x" * 6001)

    message.reply_document.assert_not_awaited()
    message.reply_text.assert_awaited_once()
    assert "too large" in message.reply_text.await_args.args[0]
    assert "saved locally" in message.reply_text.await_args.args[0]
