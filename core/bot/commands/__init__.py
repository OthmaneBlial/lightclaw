"""Composed Telegram command mixins."""

from __future__ import annotations

from .agent import CommandsAgentMixin
from .basic import CommandsBasicMixin
from .cron import CommandsCronMixin
from .heartbeat import CommandsHeartbeatMixin
from .skills import CommandsSkillsMixin


class BotCommandsMixin(
    CommandsCronMixin,
    CommandsHeartbeatMixin,
    CommandsAgentMixin,
    CommandsSkillsMixin,
    CommandsBasicMixin,
):
    """Combined command handlers for Telegram chat flows."""

    pass


__all__ = ["BotCommandsMixin"]
