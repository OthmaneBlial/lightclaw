from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config import Config
from core.bot import LightClawBot


@pytest.mark.asyncio
async def test_fallback_planner_preserves_diagnostics_without_exposing_configured_credentials():
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = Config(
        openai_api_key="fixture-private-api-key",
        telegram_bot_token="fixture-private-bot-token",
        groq_api_key="fixture-private-voice-key",
    )
    error = (
        "provider unavailable (HTTP 503); "
        f"{bot.config.openai_api_key}; {bot.config.telegram_bot_token}; "
        f"{bot.config.groq_api_key}; Bearer fixture-bearer-secret"
    )
    bot.llm = SimpleNamespace(chat=AsyncMock(side_effect=RuntimeError(error)))

    plan, plan_error = await bot._plan_multi_agent_payload(
        goal="Add tests",
        available_agents={"codex": "/fixture/codex"},
        explicit_specs=[],
        explicit_dependency_specs={},
        preferred_agents=[],
    )
    preview = bot._render_multi_plan_preview(
        goal=plan["goal"],
        workers=plan["workers"],
        plan_payload=plan["plan_payload"],
        warnings=plan["warnings"],
    )

    assert not plan_error
    assert plan["planner_mode"] == "fallback"
    assert "provider unavailable (HTTP 503)" in preview
    assert "using fallback template" in preview
    assert "[REDACTED]" in preview
    for secret in (
        bot.config.openai_api_key,
        bot.config.telegram_bot_token,
        bot.config.groq_api_key,
        "fixture-bearer-secret",
    ):
        assert secret not in preview
        assert all(secret not in warning for warning in plan["warnings"])
