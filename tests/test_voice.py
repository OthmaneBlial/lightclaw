import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

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
