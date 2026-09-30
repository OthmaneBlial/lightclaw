import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from telegram.error import NetworkError

from core.bot import LightClawBot
from core.voice import transcribe_voice

FAKE_KEY = "groq-test-key-123456789"
TRANSCRIPTION_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


def _mock_client(monkeypatch, *, response=None, error=None):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    if error:
        client.post.side_effect = error
    else:
        client.post.return_value = response
    factory = Mock(return_value=client)
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return client, factory


@pytest.mark.asyncio
async def test_missing_key_skips_http_client(monkeypatch):
    factory = Mock(side_effect=AssertionError("HTTP client must not be created"))
    monkeypatch.setattr(httpx, "AsyncClient", factory)

    assert await transcribe_voice(b"audio", "") is None
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_successful_transcription_uses_expected_request(monkeypatch):
    response = SimpleNamespace(status_code=200, json=Mock(return_value={"text": "hello"}))
    client, factory = _mock_client(monkeypatch, response=response)

    assert await transcribe_voice(b"audio-bytes", FAKE_KEY) == "hello"

    factory.assert_called_once_with(timeout=30.0)
    client.post.assert_awaited_once_with(
        TRANSCRIPTION_URL,
        headers={"Authorization": f"Bearer {FAKE_KEY}"},
        files={"file": ("audio.ogg", b"audio-bytes", "audio/ogg")},
        data={"model": "whisper-large-v3-turbo"},
    )


@pytest.mark.asyncio
async def test_non_200_response_returns_none_without_parsing(monkeypatch):
    response = SimpleNamespace(status_code=401, json=Mock(side_effect=AssertionError))
    client, _ = _mock_client(monkeypatch, response=response)

    assert await transcribe_voice(b"audio", FAKE_KEY) is None
    response.json.assert_not_called()
    client.post.assert_awaited_once()


@pytest.mark.asyncio
async def test_malformed_json_returns_none(monkeypatch, caplog):
    response = SimpleNamespace(status_code=200, json=Mock(side_effect=ValueError("bad JSON")))
    _mock_client(monkeypatch, response=response)
    caplog.set_level(logging.ERROR, logger="lightclaw")

    assert await transcribe_voice(b"audio", FAKE_KEY) is None
    assert FAKE_KEY not in caplog.text


@pytest.mark.parametrize("error_type", [httpx.TimeoutException, httpx.ConnectError])
@pytest.mark.asyncio
async def test_transport_failures_return_none_and_redact_key(monkeypatch, caplog, error_type):
    _mock_client(monkeypatch, error=error_type(f"request failed with {FAKE_KEY}"))
    caplog.set_level(logging.ERROR, logger="lightclaw")

    assert await transcribe_voice(b"audio", FAKE_KEY) is None
    assert FAKE_KEY not in caplog.text


@pytest.mark.parametrize(
    ("message_size", "download_size", "downloaded_bytes", "fetch_file"),
    [
        (5, None, b"data", False),
        (None, 5, b"data", True),
        (None, None, b"large", True),
    ],
)
@pytest.mark.asyncio
async def test_oversized_voice_is_rejected_before_transcription(
    monkeypatch, message_size, download_size, downloaded_bytes, fetch_file
):
    monkeypatch.setattr("core.bot.handlers.MAX_VOICE_FILE_BYTES", 4)
    transcribe = AsyncMock()
    monkeypatch.setattr("core.bot.handlers.transcribe_voice", transcribe)
    voice_file = SimpleNamespace(
        file_size=download_size,
        download_as_bytearray=AsyncMock(return_value=bytearray(downloaded_bytes)),
    )
    get_file = AsyncMock(return_value=voice_file)
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(groq_api_key="fixture")
    bot.is_update_allowed = lambda _update: True
    bot._pending_voice_goal_by_session = {}
    bot._reply_logged = AsyncMock()
    voice = SimpleNamespace(file_size=message_size, get_file=get_file)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456, type="private"),
        message=SimpleNamespace(voice=voice),
    )
    context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))

    await bot.handle_voice(update, context)

    assert get_file.await_count == int(fetch_file)
    if fetch_file and download_size is None:
        voice_file.download_as_bytearray.assert_awaited_once()
    else:
        voice_file.download_as_bytearray.assert_not_awaited()
    transcribe.assert_not_awaited()
    assert "transcription limit" in bot._reply_logged.await_args.args[1]


@pytest.mark.asyncio
async def test_latest_voice_request_keeps_approval_when_transcriptions_finish_out_of_order(
    monkeypatch,
):
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(groq_api_key="fixture")
    bot.is_update_allowed = lambda _update: True
    bot._session_id_from_update = lambda update: str(update.effective_chat.id)
    bot._pending_voice_goal_by_session = {}
    bot._reply_logged = AsyncMock()
    old_transcription_started = asyncio.Event()
    release_old_transcription = asyncio.Event()

    async def transcribe(audio, _key):
        if audio == b"old":
            old_transcription_started.set()
            await release_old_transcription.wait()
            return "older request"
        return "latest request"

    monkeypatch.setattr("core.bot.handlers.transcribe_voice", transcribe)

    def make_update(audio):
        voice_file = SimpleNamespace(
            file_size=None,
            download_as_bytearray=AsyncMock(return_value=bytearray(audio)),
        )
        voice = SimpleNamespace(
            file_size=None,
            get_file=AsyncMock(return_value=voice_file),
        )
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=123),
            effective_chat=SimpleNamespace(id=456, type="private"),
            message=SimpleNamespace(voice=voice, caption=""),
        )

    context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))
    older = asyncio.create_task(bot.handle_voice(make_update(b"old"), context))
    await old_transcription_started.wait()
    await bot.handle_voice(make_update(b"new"), context)
    release_old_transcription.set()
    await older

    assert bot._pending_voice_goal_by_session["456"]["transcription"] == "latest request"
    bot._reply_logged.assert_awaited_once()


@pytest.mark.asyncio
async def test_clear_revokes_pending_actions_and_discards_inflight_voice(monkeypatch):
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(groq_api_key="fixture")
    bot.is_update_allowed = lambda _update: True
    bot._session_id_from_update = lambda update: str(update.effective_chat.id)
    bot._log_user_message = Mock()
    bot._privileged_rate_limited = Mock(return_value=False)
    bot._invalidate_session_summary = Mock()
    bot._session_summaries = {"456": "old", "other": "keep"}
    bot._summary_generation_by_session = {}
    bot._pending_wipe_confirm = {"456": 10.0, "other": 20.0}
    bot._pending_multi_plan_by_session = {"other": {"goal": "keep"}}
    bot._pending_trusted_agent_run_by_session = {"other": {"task": "keep"}}
    bot._pending_voice_goal_by_session = {"other": {"text": "keep"}}
    bot._voice_request_ids_by_session = {"other": "keep"}
    bot.memory = SimpleNamespace(clear_session=Mock())
    bot._reply_logged = AsyncMock()
    transcription_started = asyncio.Event()
    finish_transcription = asyncio.Event()

    async def transcribe(_audio, _key):
        transcription_started.set()
        await finish_transcription.wait()
        return "stale voice request"

    monkeypatch.setattr("core.bot.handlers.transcribe_voice", transcribe)
    voice_file = SimpleNamespace(
        download_as_bytearray=AsyncMock(return_value=bytearray(b"audio"))
    )
    voice = SimpleNamespace(file_size=None, get_file=AsyncMock(return_value=voice_file))
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=456),
        effective_chat=SimpleNamespace(id=456, type="private"),
        message=SimpleNamespace(voice=voice, caption=""),
    )
    context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))
    voice_task = asyncio.create_task(bot.handle_voice(update, context))
    await transcription_started.wait()
    bot._pending_multi_plan_by_session["456"] = {"goal": "stale"}
    bot._pending_trusted_agent_run_by_session["456"] = {"task": "stale"}
    bot._pending_voice_goal_by_session["456"] = {"text": "stale"}

    await bot.cmd_clear(update, SimpleNamespace())
    finish_transcription.set()
    await voice_task

    assert bot._pending_wipe_confirm == {"other": 20.0}
    assert bot._pending_multi_plan_by_session == {"other": {"goal": "keep"}}
    assert bot._pending_trusted_agent_run_by_session == {"other": {"task": "keep"}}
    assert bot._pending_voice_goal_by_session == {"other": {"text": "keep"}}
    assert bot._voice_request_ids_by_session == {"other": "keep"}
    assert bot._session_summaries == {"other": "keep"}
    assert bot.memory.clear_session.call_args.args == ("456",)
    assert bot._reply_logged.await_count == 1
    assert "Pending approvals" in bot._reply_logged.await_args.args[1]


@pytest.mark.asyncio
async def test_voice_download_error_log_redacts_telegram_bot_token(caplog):
    token = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(groq_api_key="fixture", telegram_bot_token=token)
    bot.is_update_allowed = lambda _update: True
    bot._session_id_from_update = lambda _update: "456"
    bot._pending_voice_goal_by_session = {}
    bot._reply_logged = AsyncMock()
    voice = SimpleNamespace(
        file_size=None,
        get_file=AsyncMock(side_effect=NetworkError(f"request failed for {token}")),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456, type="private"),
        message=SimpleNamespace(voice=voice),
    )
    context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))
    caplog.set_level(logging.ERROR, logger="lightclaw")

    await bot.handle_voice(update, context)

    assert token not in caplog.text
    assert "request failed" in caplog.text
    assert "[REDACTED]" in caplog.text
