from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from config import Config
from core.llm.adapters import AnthropicAdapter, GeminiAdapter, OpenAICompatibleAdapter
from core.llm.contract import ProviderRequest
from providers import (
    PROVIDER_SPECS,
    LLMClient,
    ProviderError,
    ProviderErrorKind,
    ProviderResponse,
    ProviderUsage,
    validate_provider_registry,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "providers"


def _namespace(value):
    if isinstance(value, dict):
        return SimpleNamespace(**{key: _namespace(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_namespace(item) for item in value]
    return value


class _RecordedCompletions:
    def __init__(self, response):
        self.response = response
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response

    def generate_content(self, **kwargs):
        return self.create(**kwargs)


class _RecordedOpenAIClient:
    def __init__(self, response):
        self.chat = SimpleNamespace(completions=_RecordedCompletions(response))
        self.closed = 0

    def close(self):
        self.closed += 1


class _RecordedAnthropicClient:
    def __init__(self, response):
        self.messages = _RecordedCompletions(response)
        self.closed = 0

    def close(self):
        self.closed += 1


class _RecordedGeminiClient:
    def __init__(self, response):
        self.models = _RecordedCompletions(response)
        self.closed = 0

    def close(self):
        self.closed += 1


class _FixtureTypes:
    class Part:
        @staticmethod
        def from_text(*, text):
            return {"text": text}

    class Content:
        def __init__(self, **kwargs):
            self.values = kwargs

    class GenerateContentConfig:
        def __init__(self, **kwargs):
            self.values = kwargs

    class HttpRetryOptions:
        def __init__(self, **kwargs):
            self.values = kwargs

    class HttpOptions:
        def __init__(self, **kwargs):
            self.values = kwargs


def _recorded_adapter(provider: str, response):
    if provider in {"openai", "xai", "deepseek", "zai"}:
        raw_client = _RecordedOpenAIClient(response)
        return OpenAICompatibleAdapter(
            name=provider,
            model="fixture-model",
            client=raw_client,
        ), raw_client
    if provider == "claude":
        raw_client = _RecordedAnthropicClient(response)
        return AnthropicAdapter(model="fixture-model", client=raw_client), raw_client
    raw_client = _RecordedGeminiClient(response)
    return GeminiAdapter(
        model="fixture-model",
        client=raw_client,
        types_module=_FixtureTypes,
    ), raw_client


@pytest.mark.parametrize("provider", sorted(PROVIDER_SPECS))
async def test_all_six_adapters_share_recorded_fixture_contract(provider):
    fixture = json.loads((FIXTURE_ROOT / PROVIDER_SPECS[provider].fixture).read_text())
    adapter, raw_client = _recorded_adapter(provider, _namespace(fixture["response"]))
    config = Config(
        llm_provider=provider,
        llm_model="fixture-model",
        max_output_tokens=9000,
        provider_max_retries=0,
    )
    client = LLMClient(config, adapter=adapter)

    result = await client.complete(
        [{"role": "user", "content": "hello"}],
        system_prompt="system",
    )

    expected = fixture["expected"]
    assert result.provider == provider
    assert result.model == "fixture-model"
    assert result.text == expected["text"]
    assert result.attempts == 1
    for field in (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_input_tokens",
        "reasoning_tokens",
        "tool_tokens",
    ):
        assert getattr(result.usage, field) == expected.get(field)
    if provider == "deepseek":
        assert raw_client.chat.completions.calls[0]["max_tokens"] == 4096
    client.close()
    client.close()
    assert raw_client.closed == 1


@pytest.mark.parametrize("provider", ["openai", "claude", "gemini"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
async def test_nonfinite_usage_does_not_discard_provider_text(provider, value):
    fixture = json.loads((FIXTURE_ROOT / PROVIDER_SPECS[provider].fixture).read_text())
    response = fixture["response"]
    if provider == "gemini":
        response["usage_metadata"]["prompt_token_count"] = value
    else:
        field = "input_tokens" if provider == "claude" else "prompt_tokens"
        response["usage"][field] = value
    adapter, _raw_client = _recorded_adapter(provider, _namespace(response))
    try:
        result = await adapter.complete(ProviderRequest(messages=({"role": "user", "content": "hello"},)))
    finally:
        adapter.close()
    assert result.text == fixture["expected"]["text"]
    assert result.usage.input_tokens is None


class _FakeOpenAI:
    instances: list["_FakeOpenAI"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        response = _namespace(
            {
                "choices": [{"message": {"content": "ok"}}],
                "usage": None,
            }
        )
        self.chat = SimpleNamespace(completions=_RecordedCompletions(response))
        self.instances.append(self)

    def close(self):
        return None


@pytest.mark.parametrize(
    ("provider", "key_field", "base_url"),
    [
        ("openai", "openai_api_key", None),
        ("xai", "xai_api_key", "https://api.x.ai/v1"),
        ("deepseek", "deepseek_api_key", "https://api.deepseek.com"),
        ("zai", "zai_api_key", "https://api.z.ai/api/paas/v4/"),
    ],
)
async def test_openai_compatible_factories_disable_sdk_retries(
    monkeypatch,
    provider,
    key_field,
    base_url,
):
    import openai

    _FakeOpenAI.instances.clear()
    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    config = Config(llm_provider=provider, llm_model="fixture-model")
    setattr(config, key_field, "test-provider-secret")
    client = LLMClient(config)

    result = await client.complete([{"role": "user", "content": "hello"}])

    assert result.text == "ok"
    fake = _FakeOpenAI.instances[-1]
    assert fake.kwargs["api_key"] == "test-provider-secret"
    assert fake.kwargs.get("base_url") == base_url
    assert fake.kwargs["max_retries"] == 0
    assert fake.kwargs["timeout"] == 60
    client.close()


def test_anthropic_factory_disables_sdk_retries_and_closes(monkeypatch):
    import anthropic

    instances = []

    class _FakeAnthropic:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.messages = _RecordedCompletions(
                _namespace({"content": [{"text": "ok"}], "usage": {}})
            )
            self.closed = 0
            instances.append(self)

        def close(self):
            self.closed += 1

    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic)
    client = LLMClient(
        Config(
            llm_provider="claude",
            llm_model="fixture-model",
            anthropic_api_key="test-provider-secret",
        )
    )

    client.close()

    assert instances[0].kwargs["max_retries"] == 0
    assert instances[0].kwargs["timeout"] == 60
    assert instances[0].closed == 1


async def test_anthropic_compat_redirect_does_not_forward_api_key(monkeypatch):
    import httpx

    requests = []
    redirect_options = []
    real_client = httpx.Client

    def handler(request):
        requests.append((str(request.url), request.headers.get("x-api-key")))
        return httpx.Response(
            302,
            headers={"Location": "https://attacker.invalid/collect"},
            request=request,
        )

    def client_factory(*, timeout, follow_redirects):
        redirect_options.append(follow_redirects)
        return real_client(
            transport=httpx.MockTransport(handler),
            timeout=timeout,
            follow_redirects=follow_redirects,
        )

    monkeypatch.setattr(httpx, "Client", client_factory)
    adapter = AnthropicAdapter(
        model="fixture-model",
        client=object(),
        custom_base_url="https://trusted.invalid",
        api_key="fixture-secret",
    )

    with pytest.raises(RuntimeError, match="HTTP error 302"):
        await adapter.complete(
            ProviderRequest(messages=({"role": "user", "content": "hello"},))
        )

    assert redirect_options == [False]
    assert requests == [("https://trusted.invalid/v1/messages", "fixture-secret")]


def test_gemini_factory_uses_millisecond_timeout_one_attempt_and_closes(monkeypatch):
    from google import genai

    instances = []

    class _FakeGemini:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.models = _RecordedCompletions(
                _namespace({"text": "ok", "usage_metadata": None})
            )
            self.closed = 0
            instances.append(self)

        def close(self):
            self.closed += 1

    monkeypatch.setattr(genai, "Client", _FakeGemini)
    client = LLMClient(
        Config(
            llm_provider="gemini",
            llm_model="fixture-model",
            gemini_api_key="test-provider-secret",
            provider_timeout_sec=45,
        )
    )

    client.close()

    http_options = instances[0].kwargs["http_options"]
    assert http_options.timeout == 45_000
    assert http_options.retry_options.attempts == 1
    assert instances[0].closed == 1


def test_missing_optional_provider_sdk_has_actionable_install_error(monkeypatch):
    from core.llm import client as client_module

    real_import = __import__("importlib").import_module

    def missing_openai(name):
        if name == "openai":
            error = ModuleNotFoundError("No module named 'openai'")
            error.name = "openai"
            raise error
        return real_import(name)

    monkeypatch.setattr("importlib.import_module", missing_openai)

    with pytest.raises(RuntimeError, match=r"lightclaw-ai\[openai\]"):
        client_module.LLMClient(
            Config(
                llm_provider="openai",
                llm_model="fixture-model",
                openai_api_key="test-provider-secret",
            )
        )


class _StatusError(RuntimeError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code


class _PolicyAdapter:
    name = "openai"
    model = "fixture-model"

    def __init__(self, failures: list[Exception] | None = None, delay: float = 0):
        self.failures = list(failures or [])
        self.delay = delay
        self.calls = 0
        self.closed = 0

    async def complete(self, request):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failures:
            raise self.failures.pop(0)
        return ProviderResponse(
            provider=self.name,
            model=self.model,
            text="recovered",
            usage=ProviderUsage(),
        )

    def close(self):
        self.closed += 1


async def test_retry_policy_is_shared_and_reports_attempt_count(monkeypatch):
    adapter = _PolicyAdapter(
        failures=[_StatusError(429, "rate limit"), _StatusError(503, "unavailable")]
    )
    config = Config(
        llm_provider="openai",
        llm_model="fixture-model",
        provider_max_retries=2,
    )

    async def _no_delay(_seconds):
        return None

    monkeypatch.setattr("core.llm.client.asyncio.sleep", _no_delay)
    result = await LLMClient(config, adapter=adapter).complete(
        [{"role": "user", "content": "hello"}]
    )

    assert result.text == "recovered"
    assert result.attempts == 3
    assert adapter.calls == 3


async def test_failed_provider_close_can_retry_cleanup_without_accepting_work():
    fixture = json.loads((FIXTURE_ROOT / "openai.json").read_text())
    adapter, raw_client = _recorded_adapter("openai", _namespace(fixture["response"]))
    raw_client.close = Mock(side_effect=[OSError("fixture cleanup failure"), None])
    client = LLMClient(
        Config(llm_provider="openai", llm_model="fixture-model"), adapter=adapter
    )

    with pytest.raises(OSError, match="fixture cleanup failure"):
        client.close()
    with pytest.raises(RuntimeError, match="provider client is closed"):
        await client.complete([{"role": "user", "content": "hello"}])
    with pytest.raises(RuntimeError, match="provider adapter is closed"):
        await adapter.complete(ProviderRequest(messages=({"role": "user", "content": "hello"},)))
    client.close()
    client.close()
    assert raw_client.close.call_count == 2
    assert raw_client.chat.completions.calls == []


async def test_close_during_retry_delay_prevents_another_provider_attempt(monkeypatch):
    adapter = _PolicyAdapter(failures=[_StatusError(429, "rate limit")])
    client = LLMClient(
        Config(llm_provider="openai", llm_model="fixture-model", provider_max_retries=1),
        adapter=adapter,
    )

    async def close_during_delay(_seconds):
        client.close()

    monkeypatch.setattr("core.llm.client.asyncio.sleep", close_during_delay)
    with pytest.raises(RuntimeError, match="provider client is closed"):
        await client.complete([{"role": "user", "content": "hello"}])
    assert adapter.calls == 1
    assert adapter.closed == 1
    assert client.last_response is None


@pytest.mark.parametrize("route", ["openai", "claude", "gemini", "claude-compat"])
async def test_adapter_rejects_sdk_work_queued_before_close(monkeypatch, route):
    provider = "claude" if route == "claude-compat" else route
    fixture = json.loads((FIXTURE_ROOT / PROVIDER_SPECS[provider].fixture).read_text())
    adapter, raw_client = _recorded_adapter(provider, _namespace(fixture["response"]))
    if route == "claude-compat":
        adapter.custom_base_url = "https://example.invalid"
        adapter.api_key = "fixture-key"
        factory = Mock(side_effect=AssertionError("closed adapter must not open a transport"))
        monkeypatch.setattr("httpx.Client", factory)

    async def close_before_worker_starts(function, *args, **kwargs):
        adapter.close()
        return function(*args, **kwargs)

    monkeypatch.setattr("core.llm.adapters.asyncio.to_thread", close_before_worker_starts)
    with pytest.raises(RuntimeError, match="provider adapter is closed"):
        await adapter.complete(ProviderRequest(messages=({"role": "user", "content": "hello"},)))
    completions = (
        raw_client.chat.completions if provider == "openai"
        else raw_client.messages if provider == "claude" else raw_client.models
    )
    assert completions.calls == []
    assert raw_client.closed == 1
    if route == "claude-compat":
        factory.assert_not_called()


async def test_timeout_and_non_retryable_errors_are_normalized():
    timeout_adapter = _PolicyAdapter(delay=1)
    timeout_client = LLMClient(
        Config(
            llm_provider="openai",
            llm_model="fixture-model",
            provider_max_retries=0,
        ),
        adapter=timeout_adapter,
    )
    with pytest.raises(ProviderError) as timeout_error:
        await timeout_client.complete(
            [{"role": "user", "content": "hello"}],
            timeout_seconds=0.01,
        )
    assert timeout_error.value.kind == ProviderErrorKind.TIMEOUT
    assert timeout_error.value.retryable is False

    auth_adapter = _PolicyAdapter(failures=[_StatusError(401, "bad credential")])
    auth_client = LLMClient(
        Config(
            llm_provider="openai",
            llm_model="fixture-model",
            provider_max_retries=4,
        ),
        adapter=auth_adapter,
    )
    with pytest.raises(ProviderError) as auth_error:
        await auth_client.complete([{"role": "user", "content": "hello"}])
    assert auth_error.value.kind == ProviderErrorKind.AUTHENTICATION
    assert auth_error.value.retryable is False
    assert auth_adapter.calls == 1


async def test_provider_raised_timeout_retries_after_attempt_completes(monkeypatch):
    adapter = _PolicyAdapter(failures=[TimeoutError("read timeout")])
    client = LLMClient(
        Config(
            llm_provider="openai",
            llm_model="fixture-model",
            provider_max_retries=1,
        ),
        adapter=adapter,
    )

    async def no_delay(_seconds):
        return None

    monkeypatch.setattr("core.llm.client.asyncio.sleep", no_delay)

    result = await client.complete([{"role": "user", "content": "hello"}])

    assert result.text == "recovered"
    assert result.attempts == 2
    assert adapter.calls == 2


async def test_client_deadline_does_not_overlap_blocking_provider_calls():
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    lock = threading.Lock()
    calls = 0
    active = 0
    maximum_active = 0

    class BlockingAdapter:
        name = "openai"
        model = "fixture-model"

        async def complete(self, _request):
            nonlocal calls
            calls += 1

            def blocking_call():
                nonlocal active, maximum_active
                with lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                started.set()
                release.wait(2)
                with lock:
                    active -= 1
                    if not active:
                        finished.set()
                return ProviderResponse(self.name, self.model, "late", ProviderUsage())

            return await asyncio.to_thread(blocking_call)

        def close(self):
            return None

    client = LLMClient(
        Config(
            llm_provider="openai",
            llm_model="fixture-model",
            provider_max_retries=2,
        ),
        adapter=BlockingAdapter(),
    )
    try:
        with pytest.raises(ProviderError) as error:
            await client.complete(
                [{"role": "user", "content": "hello"}],
                timeout_seconds=0.05,
            )

        assert error.value.kind == ProviderErrorKind.TIMEOUT
        assert error.value.retryable is False
        assert calls == 1
        assert maximum_active == 1
        assert await asyncio.to_thread(started.wait, 1)
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)


async def test_provider_error_redacts_configured_secret(caplog):
    secret = "sk-test-super-private-value"
    adapter = _PolicyAdapter(failures=[RuntimeError(f"bad key {secret}")])
    config = Config(
        llm_provider="openai",
        llm_model="fixture-model",
        openai_api_key=secret,
        provider_max_retries=0,
    )
    client = LLMClient(config, adapter=adapter)

    result = await client.chat([{"role": "user", "content": "hello"}])

    assert secret not in result
    assert "[REDACTED]" in result
    assert secret not in caplog.text


def test_registry_requires_maintainer_and_fixture_for_every_provider():
    assert validate_provider_registry() == []
    assert set(PROVIDER_SPECS) == {"openai", "xai", "claude", "gemini", "deepseek", "zai"}
    for spec in PROVIDER_SPECS.values():
        assert (FIXTURE_ROOT / spec.fixture).is_file()
