"""Stable, vendor-neutral contract for hosted text providers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, TypedDict, runtime_checkable


class ProviderMessage(TypedDict):
    role: str
    content: str


@dataclass(frozen=True)
class ProviderRequest:
    messages: tuple[ProviderMessage, ...]
    system_prompt: str = ""
    max_output_tokens: int = 4096
    timeout_seconds: float = 60.0


@dataclass(frozen=True)
class ProviderUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    tool_tokens: int | None = None

    def as_dict(self) -> dict[str, int | None]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cache_input_tokens": self.cache_input_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "tool_tokens": self.tool_tokens,
        }


@dataclass(frozen=True)
class ProviderResponse:
    provider: str
    model: str
    text: str
    usage: ProviderUsage
    attempts: int = 1
    latency_ms: float = 0.0


class ProviderErrorKind(str, Enum):
    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    NETWORK = "network"
    QUOTA = "quota"
    INVALID_REQUEST = "invalid_request"
    UNAVAILABLE = "unavailable"
    INVALID_RESPONSE = "invalid_response"
    UNKNOWN = "unknown"


class ProviderError(RuntimeError):
    """Normalized provider failure safe for caller-side policy decisions."""

    def __init__(
        self,
        *,
        provider: str,
        kind: ProviderErrorKind,
        detail: str,
        retryable: bool,
        status_code: int | None = None,
    ) -> None:
        super().__init__(detail)
        self.provider = provider
        self.kind = kind
        self.detail = detail
        self.retryable = retryable
        self.status_code = status_code


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_delay_seconds: float = 0.25
    max_delay_seconds: float = 2.0

    def delay_for_attempt(self, attempt: int) -> float:
        exponent = max(0, int(attempt) - 1)
        return min(
            max(0.0, self.max_delay_seconds),
            max(0.0, self.initial_delay_seconds) * (2**exponent),
        )


@runtime_checkable
class ProviderAdapter(Protocol):
    name: str
    model: str

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        """Return normalized text and usage or raise a provider exception."""

    def close(self) -> None:
        """Release SDK transports; repeated calls must be harmless."""
