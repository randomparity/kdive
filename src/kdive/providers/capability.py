"""Capability value types and the provider dispatch registry (ADR-0022).

The provider seam's core: providers register capabilities keyed
``(plane, operation, resource_kind)``; the registry dispatches a requested
operation to a provider by capability match, never by name (ADR-0009). The value
types here are frozen, hashable in-memory carriers — not persisted Pydantic
models — so a :class:`Capability` can be a registry key component and an
:class:`OpContract` rejects a malformed ``cleanup`` at construction.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from kdive.domain.models import ResourceKind


class Plane(StrEnum):
    """The eight provider planes (ADR-0009). Allocation is core, not a plane."""

    DISCOVERY = "discovery"
    PROVISIONING = "provisioning"
    BUILD = "build"
    INSTALL = "install"
    CONNECT = "connect"
    DEBUG = "debug"
    CONTROL = "control"
    RETRIEVE = "retrieve"


class CleanupGuarantee(StrEnum):
    """An op's cancel/abandon cleanup guarantee (ADR-0009)."""

    CLEAN_ROLLBACK = "clean-rollback"
    BEST_EFFORT = "best-effort"
    ORPHAN_FLAGGED = "orphan-flagged"


@dataclass(frozen=True, slots=True)
class OpContract:
    """Contract flags an operation declares (ADR-0009).

    ``long_running`` routes the op as a job; ``destructive`` drives the
    destructive-op gate; ``cancelable``/``cleanup`` drive cancel and the
    reconciler.
    """

    idempotent: bool
    destructive: bool
    cancelable: bool
    long_running: bool
    cleanup: CleanupGuarantee

    def __post_init__(self) -> None:
        if not isinstance(self.cleanup, CleanupGuarantee):
            raise TypeError(
                f"cleanup must be a CleanupGuarantee, got {type(self.cleanup).__name__}"
            )


@dataclass(frozen=True, slots=True)
class Capability:
    """An advertised operation on a plane for a resource kind, with its contract."""

    plane: Plane
    operation: str
    resource_kind: ResourceKind
    contract: OpContract


@dataclass(frozen=True, slots=True)
class BoundOp:
    """A dispatched operation: the chosen provider's bound method plus its contract.

    Callers read :attr:`contract` for job routing, the destructive-op gate, and the
    reconciler without re-deriving it from the registry.
    """

    provider_id: str
    operation: str
    contract: OpContract
    call: Callable[..., object]
