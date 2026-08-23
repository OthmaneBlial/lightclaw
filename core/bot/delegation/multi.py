"""Compatibility composition for separated multi-agent planning/task domains."""

from __future__ import annotations

from .planning import DelegationMultiPlanningMixin
from .tasks import DelegationMultiTaskMixin


class DelegationMultiPlanMixin(
    DelegationMultiTaskMixin,
    DelegationMultiPlanningMixin,
):
    """Expose the historical mixin name while keeping domains independent."""

    pass
