"""Composed LightClaw bot class built from focused mixins."""

from __future__ import annotations

from .base import BotBaseMixin
from .commands import BotCommandsMixin
from .context import BotContextMixin
from .delegation import BotDelegationMixin
from .file_ops import BotFileOpsMixin
from .handlers import BotHandlersMixin
from .messaging import BotMessagingMixin


class LightClawBot(
    BotMessagingMixin,
    BotHandlersMixin,
    BotFileOpsMixin,
    BotCommandsMixin,
    BotContextMixin,
    BotDelegationMixin,
    BotBaseMixin,
):
    """The main bot class wiring Telegram, Memory, and LLM together."""

    pass


__all__ = ["LightClawBot"]
