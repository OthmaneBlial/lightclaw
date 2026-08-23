from __future__ import annotations

import os

import pytest

SENSITIVE_ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "LIGHTCLAW_CONFIG",
    "LIGHTCLAW_HOME",
    "LIGHTCLAW_PUBLIC_BOT_ACK",
    "LLM_MODEL",
    "LLM_PROVIDER",
    "OPENAI_API_KEY",
    "TELEGRAM_ALLOWED_USERS",
    "TELEGRAM_BOT_TOKEN",
    "XAI_API_KEY",
    "ZAI_API_KEY",
)


@pytest.fixture(autouse=True)
def isolated_lightclaw_environment(monkeypatch: pytest.MonkeyPatch, tmp_path):
    for key in SENSITIVE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LIGHTCLAW_CONFIG", str(tmp_path / "missing-config.env"))
    yield
    for key in SENSITIVE_ENV_KEYS:
        os.environ.pop(key, None)
