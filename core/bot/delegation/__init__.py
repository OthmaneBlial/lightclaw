"""Composed local coding-agent delegation mixins."""

from __future__ import annotations

from .agents import DelegationAgentsMixin
from .doctor import DelegationDoctorMixin
from .execution import DelegationExecutionMixin
from .multi import DelegationMultiPlanMixin
from .workspace import DelegationWorkspaceMixin


class BotDelegationMixin(
    DelegationExecutionMixin,
    DelegationWorkspaceMixin,
    DelegationDoctorMixin,
    DelegationMultiPlanMixin,
    DelegationAgentsMixin,
):
    """Combined local coding-agent delegation and doctor/check utilities."""

    pass


__all__ = ["BotDelegationMixin"]
