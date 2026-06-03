"""The reconciler loop: periodic drift repair between Postgres and libvirt (ADR-0021).

A :class:`Reconciler` owns an ``AsyncConnectionPool`` and an :class:`InfraReaper`, and
runs :func:`reconcile_once` on an interval. Each pass runs four repairs — orphaned
System, abandoned (zombie) job, dead DebugSession, leaked libvirt domain — each on a
fresh pooled connection, each fencing its writes, each isolated so one failing repair
does not starve the others. Time predicates use Postgres ``now()`` (never a Python
clock). The local-libvirt :class:`InfraReaper` implementation lands with the provider
(#15); M0 ships :class:`NullReaper` so the three Postgres-only repairs run today.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol, runtime_checkable
from uuid import UUID

_log = logging.getLogger(__name__)

DEFAULT_INTERVAL = timedelta(seconds=30)
DEFAULT_DEBUG_SESSION_STALE_AFTER = timedelta(minutes=2)

# Reserved principal for system-initiated GC teardowns (ADR-0021): a reconciler
# teardown bypasses the interactive destructive-op gate by design, made auditable
# by this attribution rather than the owning user's.
SYSTEM_RECONCILER_PRINCIPAL = "system:reconciler"


@runtime_checkable
class OwnedDomain(Protocol):
    """A libvirt domain the provider owns; ``system_id`` is its metadata tag."""

    name: str
    system_id: UUID | None


@runtime_checkable
class InfraReaper(Protocol):
    """The narrow provider port the reconciler consumes (a subset of DiscoveryPlane)."""

    async def list_owned(self) -> list[OwnedDomain]: ...
    async def destroy(self, name: str) -> None: ...


class NullReaper:
    """The M0 default reaper: owns nothing, destroys nothing.

    Until the libvirt provider (#15) ships a real :class:`InfraReaper`, this lets the
    three Postgres-only repairs run in production; leaked-domain reaping activates when
    #15 injects the real reaper. It is the honest "no provider yet" default, not a stub.
    """

    async def list_owned(self) -> list[OwnedDomain]:
        return []

    async def destroy(self, name: str) -> None:
        return None


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """Per-category counts of one pass, plus the names of repairs that raised."""

    orphaned_systems: int
    abandoned_jobs: int
    dead_sessions: int
    leaked_domains: int
    failures: tuple[str, ...]
