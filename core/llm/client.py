"""Provider registry, lifecycle, retry policy, and legacy text facade."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, replace

from config import Config
from core.security import redact_text

from .adapters import AnthropicAdapter, GeminiAdapter, OpenAICompatibleAdapter
from .contract import (
    ProviderAdapter,
    ProviderError,
    ProviderErrorKind,
    ProviderMessage,
    ProviderRequest,
    ProviderResponse,
    RetryPolicy,
)

log = logging.getLogger("lightclaw.providers")
OFFICIAL_ANTHROPIC_BASE_URL = "https://api.anthropic.com"


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    maintainer: str
    transport: str
    sdk: str
    credential_field: str
    base_url: str | None
    fixture: str
    max_output_tokens: int | None = None


PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "openai": ProviderSpec(
        name="openai",
        maintainer="OthmaneBlial",
        transport="native-chat-completions",
        sdk="openai",
        credential_field="openai_api_key",
        base_url=None,
        fixture="openai.json",
    ),
    "xai": ProviderSpec(
        name="xai",
        maintainer="OthmaneBlial",
        transport="openai-compatible",
        sdk="openai",
        credential_field="xai_api_key",
        base_url="https://api.x.ai/v1",
        fixture="xai.json",
    ),
    "claude": ProviderSpec(
        name="claude",
        maintainer="OthmaneBlial",
        transport="native-messages",
        sdk="anthropic",
        credential_field="anthropic_api_key|anthropic_auth_token",
        base_url=OFFICIAL_ANTHROPIC_BASE_URL,
        fixture="claude.json",
    ),
    "gemini": ProviderSpec(
        name="gemini",
        maintainer="OthmaneBlial",
        transport="native-generate-content",
        sdk="google-genai",
        credential_field="gemini_api_key",
        base_url=None,
        fixture="gemini.json",
    ),
    "deepseek": ProviderSpec(
        name="deepseek",
        maintainer="OthmaneBlial",
        transport="openai-compatible",
        sdk="openai",
        credential_field="deepseek_api_key",
        base_url="https://api.deepseek.com",
        fixture="deepseek.json",
        max_output_tokens=4096,
    ),
    "zai": ProviderSpec(
        name="zai",
        maintainer="OthmaneBlial",
        transport="openai-compatible",
        sdk="openai",
        credential_field="zai_api_key",
        base_url="https://api.z.ai/api/paas/v4/",
        fixture="zai.json",
    ),
}


def validate_provider_registry() -> list[str]:
    errors: list[str] = []
    for key, spec in PROVIDER_SPECS.items():
        if key != spec.name:
            errors.append(f"provider key/name mismatch: {key}/{spec.name}")
        if not spec.maintainer.strip():
            errors.append(f"provider {key} has no maintainer")
        if not spec.fixture.endswith(".json"):
            errors.append(f"provider {key} has no recorded JSON fixture")
    return errors


def _status_code(error: Exception) -> int | None:
    value = getattr(error, "status_code", None)
    if isinstance(value, bool) or not isinstance(value, int):
        response = getattr(error, "response", None)
        value = getattr(response, "status_code", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _normalize_error(provider: str, error: Exception, config: Config) -> ProviderError:
    detail = redact_text(str(error) or error.__class__.__name__, vars(config))
    lowered = detail.lower()
    class_name = error.__class__.__name__.lower()
    status = _status_code(error)

    if provider == "zai" and any(
        marker in detail for marker in ("1113", "余额不足", "无可用资源包")
    ):
        kind = ProviderErrorKind.QUOTA
    elif status in {401, 403} or any(
        marker in lowered for marker in ("authentication", "unauthorized", "invalid api key")
    ):
        kind = ProviderErrorKind.AUTHENTICATION
    elif status == 429 or "rate limit" in lowered or "ratelimit" in class_name:
        kind = ProviderErrorKind.RATE_LIMIT
    elif status == 408 or "timeout" in class_name or "timed out" in lowered or "timeout" in lowered:
        kind = ProviderErrorKind.TIMEOUT
    elif any(
        marker in lowered
        for marker in ("connection error", "connection refused", "network", "dns")
    ) or "connection" in class_name:
        kind = ProviderErrorKind.NETWORK
    elif any(
        marker in lowered
        for marker in ("quota", "billing", "insufficient balance", "resource exhausted")
    ):
        kind = ProviderErrorKind.QUOTA
    elif status == 409:
        kind = ProviderErrorKind.UNAVAILABLE
    elif status in {400, 404, 413, 422} or "badrequest" in class_name:
        kind = ProviderErrorKind.INVALID_REQUEST
    elif status is not None and status >= 500:
        kind = ProviderErrorKind.UNAVAILABLE
    elif isinstance(error, (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError)):
        kind = ProviderErrorKind.INVALID_RESPONSE
    else:
        kind = ProviderErrorKind.UNKNOWN

    retryable = kind in {
        ProviderErrorKind.RATE_LIMIT,
        ProviderErrorKind.TIMEOUT,
        ProviderErrorKind.NETWORK,
        ProviderErrorKind.UNAVAILABLE,
    } or status in {408, 409}
    return ProviderError(
        provider=provider,
        kind=kind,
        detail=detail[:800],
        retryable=retryable,
        status_code=status,
    )


def _format_legacy_error(error: ProviderError) -> str:
    prefix = f"⚠️ Error communicating with {error.provider}: "
    if error.kind == ProviderErrorKind.RATE_LIMIT:
        return prefix + "rate limit hit. Please retry in a moment."
    if error.kind == ProviderErrorKind.TIMEOUT:
        return prefix + "request timed out after bounded retries."
    if error.kind == ProviderErrorKind.QUOTA:
        return prefix + "account quota or balance is exhausted."
    if error.kind == ProviderErrorKind.AUTHENTICATION:
        return prefix + "authentication was rejected; verify the configured credential."
    if error.kind in {ProviderErrorKind.NETWORK, ProviderErrorKind.UNAVAILABLE}:
        return prefix + "provider is temporarily unavailable after bounded retries."
    return prefix + error.detail


class LLMClient:
    """Typed provider client with one timeout, retry, usage, and error contract."""

    def __init__(self, config: Config, adapter: ProviderAdapter | None = None):
        self.config = config
        self.provider_name = config.llm_provider.strip().lower()
        self.model = config.llm_model
        self.timeout_seconds = max(
            1.0,
            min(600.0, float(getattr(config, "provider_timeout_sec", 60) or 60)),
        )
        max_retries = max(
            0,
            min(4, int(getattr(config, "provider_max_retries", 2) or 0)),
        )
        self.retry_policy = RetryPolicy(max_attempts=max_retries + 1)
        self.max_output_tokens = max(
            256,
            int(getattr(config, "max_output_tokens", 4096) or 4096),
        )
        spec = PROVIDER_SPECS.get(self.provider_name)
        if not spec:
            raise ValueError(
                f"Unknown provider: {self.provider_name!r}. Supported: "
                + ", ".join(PROVIDER_SPECS)
            )
        if spec.max_output_tokens is not None:
            self.max_output_tokens = min(self.max_output_tokens, spec.max_output_tokens)
        self._adapter = adapter or self._build_adapter(spec)
        self._closed = False
        self.last_response: ProviderResponse | None = None
        log.info(
            "Initialized %s provider (model=%s, timeout=%ss, attempts=%s)",
            self.provider_name,
            self.model,
            self.timeout_seconds,
            self.retry_policy.max_attempts,
        )

    def _build_adapter(self, spec: ProviderSpec) -> ProviderAdapter:
        if spec.transport in {"native-chat-completions", "openai-compatible"}:
            import openai

            api_key = str(getattr(self.config, spec.credential_field, "") or "").strip()
            if not api_key:
                raise ValueError(
                    f"{spec.credential_field.upper()} is required when LLM_PROVIDER={spec.name}"
                )
            kwargs: dict[str, object] = {
                "api_key": api_key,
                "max_retries": 0,
                "timeout": self.timeout_seconds,
            }
            if spec.base_url:
                kwargs["base_url"] = spec.base_url
            client = openai.OpenAI(**kwargs)
            return OpenAICompatibleAdapter(name=spec.name, model=self.model, client=client)

        if spec.name == "claude":
            import anthropic

            api_key = (self.config.anthropic_api_key or "").strip()
            auth_token = (self.config.anthropic_auth_token or "").strip()
            if not api_key and not auth_token:
                raise ValueError(
                    "ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN is required when "
                    "LLM_PROVIDER=claude"
                )
            base_url = (self.config.anthropic_base_url or OFFICIAL_ANTHROPIC_BASE_URL).rstrip("/")
            kwargs: dict[str, object] = {
                "base_url": base_url,
                "max_retries": 0,
                "timeout": self.timeout_seconds,
            }
            if auth_token:
                kwargs["auth_token"] = auth_token
            else:
                kwargs["api_key"] = api_key
            client = anthropic.Anthropic(**kwargs)
            custom_base_url = "" if base_url == OFFICIAL_ANTHROPIC_BASE_URL else base_url
            return AnthropicAdapter(
                model=self.model,
                client=client,
                custom_base_url=custom_base_url,
                api_key=api_key,
                auth_token=auth_token,
            )

        if spec.name == "gemini":
            from google import genai
            from google.genai import types

            api_key = (self.config.gemini_api_key or "").strip()
            if not api_key:
                raise ValueError("GEMINI_API_KEY is required when LLM_PROVIDER=gemini")
            http_options = types.HttpOptions(
                timeout=int(self.timeout_seconds * 1000),
                retry_options=types.HttpRetryOptions(attempts=1),
            )
            client = genai.Client(api_key=api_key, http_options=http_options)
            return GeminiAdapter(model=self.model, client=client, types_module=types)

        raise ValueError(f"No adapter factory for provider: {spec.name}")

    @staticmethod
    def _normalize_messages(messages: list[dict]) -> tuple[ProviderMessage, ...]:
        normalized: list[ProviderMessage] = []
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                raise ValueError(f"message {index} must be an object")
            role = str(message.get("role") or "").strip().lower()
            content = message.get("content")
            if role not in {"user", "assistant"} or not isinstance(content, str):
                raise ValueError(
                    f"message {index} requires role user/assistant and string content"
                )
            normalized.append({"role": role, "content": content})
        return tuple(normalized)

    async def complete(
        self,
        messages: list[dict],
        system_prompt: str = "",
        max_output_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> ProviderResponse:
        if self._closed:
            raise RuntimeError("provider client is closed")
        timeout = max(
            0.01,
            min(600.0, float(timeout_seconds or self.timeout_seconds)),
        )
        output_tokens = max(256, int(max_output_tokens or self.max_output_tokens))
        ceiling = PROVIDER_SPECS[self.provider_name].max_output_tokens
        if ceiling is not None:
            output_tokens = min(output_tokens, ceiling)
        request = ProviderRequest(
            messages=self._normalize_messages(messages),
            system_prompt=str(system_prompt or ""),
            max_output_tokens=output_tokens,
            timeout_seconds=timeout,
        )

        last_error: ProviderError | None = None
        for attempt in range(1, self.retry_policy.max_attempts + 1):
            try:
                response = await asyncio.wait_for(
                    self._adapter.complete(request),
                    timeout=timeout,
                )
                response = replace(response, attempts=attempt)
                self.last_response = response
                return response
            except asyncio.TimeoutError:
                last_error = ProviderError(
                    provider=self.provider_name,
                    kind=ProviderErrorKind.TIMEOUT,
                    detail=f"request exceeded {timeout:g} seconds",
                    retryable=True,
                )
            except ProviderError as error:
                last_error = error
            except Exception as error:
                last_error = _normalize_error(self.provider_name, error, self.config)

            log.warning(
                "Provider attempt failed provider=%s attempt=%s/%s kind=%s retryable=%s",
                self.provider_name,
                attempt,
                self.retry_policy.max_attempts,
                last_error.kind.value,
                last_error.retryable,
            )
            if not last_error.retryable or attempt >= self.retry_policy.max_attempts:
                raise last_error
            await asyncio.sleep(self.retry_policy.delay_for_attempt(attempt))

        raise last_error or RuntimeError("provider request failed without an error")

    async def chat(
        self,
        messages: list[dict],
        system_prompt: str = "",
        max_output_tokens: int | None = None,
    ) -> str:
        """Backward-compatible text-only facade used by current bot handlers."""
        try:
            response = await self.complete(
                messages,
                system_prompt,
                max_output_tokens=max_output_tokens,
            )
            return response.text
        except ProviderError as error:
            log.error(
                "Provider request failed provider=%s kind=%s status=%s detail=%s",
                error.provider,
                error.kind.value,
                error.status_code,
                error.detail,
            )
            return _format_legacy_error(error)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._adapter.close()
