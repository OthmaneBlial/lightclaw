"""Deterministic test adapters; none of these contact Telegram or paid providers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock


class FakeLLM:
    def __init__(self, responses: list[str] | None = None):
        self.responses = list(responses or ["fixture response"])
        self.calls: list[dict[str, object]] = []

    async def chat(self, messages, system_prompt="", max_output_tokens=None):
        self.calls.append(
            {
                "messages": messages,
                "system_prompt": system_prompt,
                "max_output_tokens": max_output_tokens,
            }
        )
        return self.responses.pop(0) if self.responses else "fixture response"


def fake_telegram_update(user_id: int = 123, chat_id: int = 456):
    message = SimpleNamespace(
        text="fixture",
        voice=None,
        photo=[],
        document=None,
        caption="",
        reply_text=AsyncMock(),
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id),
        message=message,
    )
