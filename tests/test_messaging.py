from __future__ import annotations

import asyncio
import logging
import threading
from datetime import timedelta
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter

import core.bot.messaging as messaging
from core.bot.base import BotBaseMixin
from core.bot.messaging import BotMessagingMixin


class MessagingHarness(BotMessagingMixin):
    @staticmethod
    def _session_id_from_update(_update) -> str:
        return "fixture-session"

    @staticmethod
    def _log_bot_message(_session_id: str, _text: str) -> None:
        pass


class ReplyHarness(BotBaseMixin):
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
@pytest.mark.parametrize("oversized", [False, True])
async def test_send_response_redacts_configured_secret_values(tmp_path, oversized):
    secret = "configured-provider-value-7f3a"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bot = MessagingHarness()
    bot.config = SimpleNamespace(
        openai_api_key=secret,
        workspace_path=str(workspace),
    )
    text = "Provider returned " + secret
    if oversized:
        text += "\nstatus: complete" + "\ncontext" * 1000
    placeholder = SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(reply_document=AsyncMock())

    await bot._send_response(
        None if oversized else placeholder,
        SimpleNamespace(message=message),
        text,
    )

    if oversized:
        artifact = next((workspace / ".lightclaw-meta" / "messages").glob("response-*.md"))
        result_text = artifact.read_text()
    else:
        result_text = placeholder.edit_text.await_args.args[0]
    assert secret not in result_text
    assert "[REDACTED]" in result_text


@pytest.mark.asyncio
async def test_reply_logged_redacts_configured_secret_values():
    secret = "configured-provider-value-7f3a"
    bot = ReplyHarness.__new__(ReplyHarness)
    bot.config = SimpleNamespace(openai_api_key=secret)
    message = SimpleNamespace(reply_text=AsyncMock())

    await bot._reply_logged(
        SimpleNamespace(message=message),
        "Provider returned " + secret,
    )

    text = message.reply_text.await_args.args[0]
    assert secret not in text
    assert "[REDACTED]" in text


@pytest.mark.asyncio
async def test_network_error_log_redacts_telegram_bot_token(caplog):
    token = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
    bot = MessagingHarness()
    bot.config = SimpleNamespace(telegram_bot_token=token)
    caplog.set_level(logging.WARNING, logger="lightclaw")

    await bot.on_error(object(), SimpleNamespace(error=NetworkError(f"request failed for {token}")))

    assert token not in caplog.text
    assert "request failed" in caplog.text
    assert "[REDACTED]" in caplog.text


@pytest.mark.asyncio
async def test_rate_limit_log_formats_retry_after_timedelta(caplog):
    bot = MessagingHarness()
    caplog.set_level(logging.WARNING, logger="lightclaw")

    await bot.on_error(object(), SimpleNamespace(error=RetryAfter(120)))

    assert "Telegram rate limit: retry after 0:02:00" in caplog.text


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
async def test_short_rate_limit_retries_message_once(monkeypatch):
    bot = MessagingHarness()
    send = AsyncMock(side_effect=[RetryAfter(timedelta(seconds=2)), None])
    sleep = AsyncMock()
    monkeypatch.setattr(messaging.asyncio, "sleep", sleep)

    assert await bot._try_send(send, "hello")

    assert send.await_count == 2
    sleep.assert_awaited_once_with(2.0)


@pytest.mark.asyncio
async def test_long_rate_limit_propagates_without_holding_message_handler(monkeypatch):
    bot = MessagingHarness()
    send = AsyncMock(side_effect=RetryAfter(timedelta(seconds=31)))
    sleep = AsyncMock()
    monkeypatch.setattr(messaging.asyncio, "sleep", sleep)

    with pytest.raises(RetryAfter):
        await bot._try_send(send, "hello")

    send.assert_awaited_once()
    sleep.assert_not_awaited()


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


@pytest.mark.asyncio
async def test_long_response_artifact_write_runs_off_event_loop(tmp_path, monkeypatch):
    bot = MessagingHarness()
    bot.config = SimpleNamespace(workspace_path=str(tmp_path / "workspace"))
    loop_thread = threading.get_ident()
    worker_threads = {}
    write_artifact = bot._write_long_response_artifact
    open_artifact = bot._open_long_response_artifact

    def track_write(text):
        worker_threads["write"] = threading.get_ident()
        return write_artifact(text)

    def track_open(path):
        worker_threads["open"] = threading.get_ident()
        return open_artifact(path)

    monkeypatch.setattr(bot, "_write_long_response_artifact", track_write)
    monkeypatch.setattr(bot, "_open_long_response_artifact", track_open)
    message = SimpleNamespace(reply_document=AsyncMock())

    await bot._send_response(None, SimpleNamespace(message=message), "evidence\n" * 1000)

    assert {"write", "open"} <= worker_threads.keys()
    assert all(thread_id != loop_thread for thread_id in worker_threads.values())


@pytest.mark.asyncio
async def test_long_response_open_closes_handle_when_cancelled(tmp_path, monkeypatch):
    bot = MessagingHarness()
    bot.config = SimpleNamespace(workspace_path=str(tmp_path / "workspace"))
    open_started = threading.Event()
    open_release = threading.Event()
    opened_handles = []
    open_artifact = bot._open_long_response_artifact

    def delayed_open(path):
        open_started.set()
        open_release.wait()
        result = open_artifact(path)
        opened_handles.append(result[0])
        return result

    monkeypatch.setattr(bot, "_open_long_response_artifact", delayed_open)
    message = SimpleNamespace(reply_document=AsyncMock())
    task = asyncio.create_task(
        bot._send_response(None, SimpleNamespace(message=message), "evidence\n" * 1000)
    )
    try:
        assert await asyncio.to_thread(open_started.wait, 5)
        task.cancel()
    finally:
        open_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(opened_handles) == 1
    assert opened_handles[0].closed


@pytest.mark.asyncio
async def test_long_response_attachment_rejects_symlink_swap(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "private.txt"
    outside.write_text("private data", encoding="utf-8")
    bot = MessagingHarness()
    bot.config = SimpleNamespace(workspace_path=str(workspace))
    write_artifact = bot._write_long_response_artifact

    def replace_artifact_with_symlink(text):
        artifact = write_artifact(text)
        artifact.unlink()
        artifact.symlink_to(outside)
        return artifact

    monkeypatch.setattr(bot, "_write_long_response_artifact", replace_artifact_with_symlink)
    message = SimpleNamespace(reply_document=AsyncMock(), reply_text=AsyncMock())

    await bot._send_response(None, SimpleNamespace(message=message), "evidence\n" * 1000)

    message.reply_document.assert_not_awaited()
    message.reply_text.assert_awaited_once()
    assert "could not be checked" in message.reply_text.await_args.args[0]


def test_long_response_artifact_rejects_symlinked_parent_swap(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    messages = workspace / ".lightclaw-meta" / "messages"
    outside = tmp_path / "outside"
    messages.mkdir(parents=True)
    outside.mkdir()
    bot = MessagingHarness()
    bot.config = SimpleNamespace(workspace_path=str(workspace))
    write = messaging.atomic_write_text_at

    def swap_then_write(root, relative, content, **kwargs):
        messages.rename(messages.with_name("messages-saved"))
        messages.symlink_to(outside, target_is_directory=True)
        return write(root, relative, content, **kwargs)

    monkeypatch.setattr(messaging, "atomic_write_text_at", swap_then_write)

    with pytest.raises(OSError):
        bot._write_long_response_artifact("long result")

    assert not list(outside.iterdir())
