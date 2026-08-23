"""Compatibility composition for separated agent command domains."""

from __future__ import annotations

from .agent_acceptance import CommandsAgentAcceptanceMixin
from .agent_execution import CommandsAgentExecutionMixin
from .agent_router import CommandsAgentRouterMixin


class CommandsAgentMixin(
    CommandsAgentExecutionMixin,
    CommandsAgentAcceptanceMixin,
    CommandsAgentRouterMixin,
):
    """Expose the historical command mixin while separating responsibilities."""

    pass
