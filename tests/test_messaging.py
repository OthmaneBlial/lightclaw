from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.constants import ParseMode

from core.bot.messaging import BotMessagingMixin


class MessagingHarness(BotMessagingMixin):
    @staticmethod
    def _session_id_from_update(_update) -> str:
        return "fixture-session"

    @staticmethod
    def _log_bot_message(_session_id: str, _text: str) -> None:
        pass


def test_chunk_message_respects_utf16_limit_and_preserves_content():
    text = "😀" * 2501 + "\n" + "x" * 25

    chunks = MessagingHarness._chunk_message(text, max_len=3000)

    assert len(chunks) > 1
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 3000 for chunk in chunks)
    assert "".join(chunks) == text


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
async def test_plain_fallback_decodes_escaped_html_entities():
    bot = MessagingHarness()
    calls: list[tuple[str, str | None]] = []

    async def send(text: str, parse_mode: str | None = None):
        calls.append((text, parse_mode))
        if parse_mode:
            raise ValueError("bad markup")

    assert await bot._try_send(send, "A &lt; B &amp; <b>bold</b>")
    assert calls == [
        ("A &lt; B &amp; <b>bold</b>", ParseMode.HTML),
        ("A < B & bold", None),
    ]
