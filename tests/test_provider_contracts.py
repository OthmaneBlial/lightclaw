from __future__ import annotations

from types import SimpleNamespace

import pytest

from config import Config
from providers import LLMClient


class FakeCompletions:
    def __init__(self, response_text: str = "ok", error: Exception | None = None):
        self.response_text = response_text
        self.error = error
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.response_text))]
        )


class FakeOpenAI:
    instances: list["FakeOpenAI"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.chat = SimpleNamespace(completions=FakeCompletions())
        self.instances.append(self)


@pytest.mark.parametrize(
    ("provider", "key_field", "base_url"),
    [
        ("openai", "openai_api_key", None),
        ("xai", "xai_api_key", "https://api.x.ai/v1"),
        ("deepseek", "deepseek_api_key", "https://api.deepseek.com"),
        ("zai", "zai_api_key", "https://open.bigmodel.cn/api/paas/v4"),
    ],
)
async def test_openai_compatible_provider_routing(monkeypatch, provider, key_field, base_url):
    import openai

    FakeOpenAI.instances.clear()
    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    config = Config(llm_provider=provider, llm_model="fixture-model")
    setattr(config, key_field, "test-provider-secret")

    client = LLMClient(config)
    result = await client.chat([{"role": "user", "content": "hello"}], "system")

    assert result == "ok"
    fake = FakeOpenAI.instances[-1]
    assert fake.kwargs["api_key"] == "test-provider-secret"
    assert fake.kwargs.get("base_url") == base_url
    call = fake.chat.completions.calls[-1]
    assert call["model"] == "fixture-model"
    assert call["messages"][0] == {"role": "system", "content": "system"}


async def test_provider_error_redacts_configured_secret(monkeypatch, caplog):
    import openai

    FakeOpenAI.instances.clear()
    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    secret = "sk-test-super-private-value"
    config = Config(
        llm_provider="openai",
        llm_model="fixture-model",
        openai_api_key=secret,
    )
    client = LLMClient(config)
    client._client.chat.completions = FakeCompletions(error=RuntimeError(f"bad key {secret}"))

    result = await client.chat([{"role": "user", "content": "hello"}])

    assert secret not in result
    assert "[REDACTED]" in result
    assert secret not in caplog.text
