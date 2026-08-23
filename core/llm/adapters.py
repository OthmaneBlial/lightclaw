"""Vendor-specific request/response mapping behind the provider protocol."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from .contract import ProviderRequest, ProviderResponse, ProviderUsage


def _token_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, int(value))


def _usage_value(usage: object, name: str) -> int | None:
    return _token_count(getattr(usage, name, None)) if usage is not None else None


class _ClosableAdapter:
    def __init__(self) -> None:
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self.client, "close", None)
        if callable(close):
            close()


class OpenAICompatibleAdapter(_ClosableAdapter):
    """Map OpenAI Chat Completions and compatible endpoints to one response."""

    def __init__(self, *, name: str, model: str, client: Any) -> None:
        super().__init__()
        self.name = name
        self.model = model
        self.client = client

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        messages: list[dict[str, str]] = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.extend(dict(message) for message in request.messages)
        started = time.perf_counter()
        response = await asyncio.to_thread(
            self.client.chat.completions.create,
            model=self.model,
            messages=messages,
            max_tokens=request.max_output_tokens,
            temperature=0.7,
            timeout=request.timeout_seconds,
        )
        choices = getattr(response, "choices", None) or []
        message = getattr(choices[0], "message", None) if choices else None
        text = getattr(message, "content", "") if message is not None else ""
        usage = getattr(response, "usage", None)
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        completion_details = getattr(usage, "completion_tokens_details", None)
        return ProviderResponse(
            provider=self.name,
            model=self.model,
            text=text if isinstance(text, str) else "",
            usage=ProviderUsage(
                input_tokens=_usage_value(usage, "prompt_tokens"),
                output_tokens=_usage_value(usage, "completion_tokens"),
                total_tokens=_usage_value(usage, "total_tokens"),
                cache_input_tokens=_usage_value(prompt_details, "cached_tokens"),
                reasoning_tokens=_usage_value(completion_details, "reasoning_tokens"),
            ),
            latency_ms=(time.perf_counter() - started) * 1000,
        )


class AnthropicAdapter(_ClosableAdapter):
    """Map Anthropic Messages or an explicit compatible base URL."""

    def __init__(
        self,
        *,
        model: str,
        client: Any,
        custom_base_url: str = "",
        api_key: str = "",
        auth_token: str = "",
    ) -> None:
        super().__init__()
        self.name = "claude"
        self.model = model
        self.client = client
        self.custom_base_url = custom_base_url.rstrip("/")
        self.api_key = api_key
        self.auth_token = auth_token

    @staticmethod
    def _messages(request: ProviderRequest) -> list[dict[str, str]]:
        messages = [
            dict(message)
            for message in request.messages
            if message.get("role") in {"user", "assistant"}
        ]
        if not messages or messages[0]["role"] != "user":
            messages.insert(0, {"role": "user", "content": "Hello!"})
        return messages

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        if self.custom_base_url:
            return await self._complete_http_compat(request)
        messages = self._messages(request)
        kwargs: dict[str, object] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": request.max_output_tokens,
            "timeout": request.timeout_seconds,
        }
        if request.system_prompt:
            kwargs["system"] = request.system_prompt
        started = time.perf_counter()
        response = await asyncio.to_thread(self.client.messages.create, **kwargs)
        text = "\n".join(
            str(block.text)
            for block in (getattr(response, "content", None) or [])
            if isinstance(getattr(block, "text", None), str)
        )
        usage = getattr(response, "usage", None)
        base_input_tokens = _usage_value(usage, "input_tokens")
        cache_creation_tokens = _usage_value(usage, "cache_creation_input_tokens")
        cache_read_tokens = _usage_value(usage, "cache_read_input_tokens")
        cache_values = [
            value
            for value in (cache_creation_tokens, cache_read_tokens)
            if value is not None
        ]
        cache_input_tokens = sum(cache_values) if cache_values else None
        input_tokens = (
            base_input_tokens + (cache_input_tokens or 0)
            if base_input_tokens is not None
            else None
        )
        output_tokens = _usage_value(usage, "output_tokens")
        total_tokens = (
            input_tokens + output_tokens
            if input_tokens is not None and output_tokens is not None
            else None
        )
        return ProviderResponse(
            provider=self.name,
            model=self.model,
            text=text,
            usage=ProviderUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                cache_input_tokens=cache_input_tokens,
            ),
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    async def _complete_http_compat(self, request: ProviderRequest) -> ProviderResponse:
        import httpx

        headers = {
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        if self.auth_token:
            headers["authorization"] = f"Bearer {self.auth_token}"
        elif self.api_key:
            headers["x-api-key"] = self.api_key
        else:
            raise RuntimeError("missing Claude credentials")
        payload: dict[str, object] = {
            "model": self.model,
            "messages": self._messages(request),
            "max_tokens": request.max_output_tokens,
        }
        if request.system_prompt:
            payload["system"] = request.system_prompt

        def _post() -> tuple[int, str]:
            with httpx.Client(
                timeout=request.timeout_seconds,
                follow_redirects=True,
            ) as client:
                response = client.post(
                    f"{self.custom_base_url}/v1/messages",
                    headers=headers,
                    json=payload,
                )
            return response.status_code, response.text

        started = time.perf_counter()
        status_code, body_text = await asyncio.to_thread(_post)
        if status_code >= 400:
            detail = body_text.strip().replace("\n", " ")[:240]
            error = RuntimeError(
                f"Claude compatibility HTTP error {status_code}: {detail or 'empty response'}"
            )
            error.status_code = status_code  # type: ignore[attr-defined]
            raise error
        data = json.loads(body_text)
        content = data.get("content") if isinstance(data, dict) else None
        text = "\n".join(
            str(block.get("text"))
            for block in (content if isinstance(content, list) else [])
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
        raw_usage = data.get("usage") if isinstance(data, dict) else None
        base_input_tokens = _token_count(
            raw_usage.get("input_tokens") if isinstance(raw_usage, dict) else None
        )
        cache_creation_tokens = _token_count(
            raw_usage.get("cache_creation_input_tokens")
            if isinstance(raw_usage, dict)
            else None
        )
        cache_read_tokens = _token_count(
            raw_usage.get("cache_read_input_tokens")
            if isinstance(raw_usage, dict)
            else None
        )
        cache_values = [
            value
            for value in (cache_creation_tokens, cache_read_tokens)
            if value is not None
        ]
        cache_input_tokens = sum(cache_values) if cache_values else None
        input_tokens = (
            base_input_tokens + (cache_input_tokens or 0)
            if base_input_tokens is not None
            else None
        )
        output_tokens = _token_count(
            raw_usage.get("output_tokens") if isinstance(raw_usage, dict) else None
        )
        total_tokens = (
            input_tokens + output_tokens
            if input_tokens is not None and output_tokens is not None
            else None
        )
        return ProviderResponse(
            provider=self.name,
            model=self.model,
            text=text,
            usage=ProviderUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                cache_input_tokens=cache_input_tokens,
            ),
            latency_ms=(time.perf_counter() - started) * 1000,
        )


class GeminiAdapter(_ClosableAdapter):
    """Map the current Google Gen AI SDK to the provider contract."""

    def __init__(self, *, model: str, client: Any, types_module: Any) -> None:
        super().__init__()
        self.name = "gemini"
        self.model = model
        self.client = client
        self.types = types_module

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        contents = []
        for message in request.messages:
            role = "model" if message.get("role") == "assistant" else "user"
            contents.append(
                self.types.Content(
                    role=role,
                    parts=[self.types.Part.from_text(text=message.get("content", ""))],
                )
            )
        if not contents:
            contents.append(
                self.types.Content(
                    role="user",
                    parts=[self.types.Part.from_text(text="Hello!")],
                )
            )
        config = self.types.GenerateContentConfig(
            system_instruction=request.system_prompt or None,
            max_output_tokens=request.max_output_tokens,
            temperature=0.7,
            http_options=self.types.HttpOptions(
                timeout=int(request.timeout_seconds * 1000),
                retry_options=self.types.HttpRetryOptions(attempts=1),
            ),
        )
        started = time.perf_counter()
        response = await asyncio.to_thread(
            self.client.models.generate_content,
            model=self.model,
            contents=contents,
            config=config,
        )
        usage = getattr(response, "usage_metadata", None)
        return ProviderResponse(
            provider=self.name,
            model=self.model,
            text=str(getattr(response, "text", "") or ""),
            usage=ProviderUsage(
                input_tokens=_usage_value(usage, "prompt_token_count"),
                output_tokens=_usage_value(usage, "candidates_token_count"),
                total_tokens=_usage_value(usage, "total_token_count"),
                cache_input_tokens=_usage_value(usage, "cached_content_token_count"),
                reasoning_tokens=_usage_value(usage, "thoughts_token_count"),
                tool_tokens=_usage_value(usage, "tool_use_prompt_token_count"),
            ),
            latency_ms=(time.perf_counter() - started) * 1000,
        )
