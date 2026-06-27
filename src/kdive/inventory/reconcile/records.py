"""Shared reconcile records and ownership constants."""

from __future__ import annotations

from dataclasses import dataclass, field

from kdive.domain.catalog.resources import ManagedBy

CONFIG_MANAGED_BY = ManagedBy.CONFIG.value
DISCOVERY_MANAGED_BY = ManagedBy.DISCOVERY.value


@dataclass(frozen=True)
class ReconcileRecord:
    """One reconciled or warned entity, identified for the operator-facing diff.

    Args:
        name: The entity's stable name.
        entry: A human-readable identity for warnings.
        detail: An optional short reason.
    """

    name: str
    entry: str
    detail: str = ""


@dataclass
class ReconcileDiff:
    """The outcome of one reconcile pass, per entity type."""

    created: list[ReconcileRecord] = field(default_factory=list)
    updated: list[ReconcileRecord] = field(default_factory=list)
    pruned: list[ReconcileRecord] = field(default_factory=list)
    cordoned: list[ReconcileRecord] = field(default_factory=list)
    warned: list[ReconcileRecord] = field(default_factory=list)


@dataclass(frozen=True)
class PruneOutcome:
    """A prune/cordon decision for one candidate row."""

    pruned: bool
    cordoned: bool


__all__ = [
    "CONFIG_MANAGED_BY",
    "DISCOVERY_MANAGED_BY",
    "ManagedBy",
    "PruneOutcome",
    "ReconcileDiff",
    "ReconcileRecord",
]
