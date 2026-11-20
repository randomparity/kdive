"""The mock infra-inventory seam (ADR-0072).

The fault-inject provider owns no real infrastructure, so it tracks the synthetic domains it
provisions in an in-process :class:`FaultInjectInventory`. The :class:`FaultInjectReaper`
exposes that inventory in the provider reaper shape (async ``list_owned``/``destroy``), so
the leaked-domain repair pass has synthetic infra to find and reap.

The inventory is shared by reference: the provisioner records a domain, teardown/control
forgets it, and the reaper reads the same map — so a domain that outlives its System row
is a *leaked* domain the reaper reports.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from uuid import UUID

from kdive.providers.infra.reaping import OwnedDomain as ReaperOwnedDomain


@dataclass(frozen=True, slots=True)
class OwnedDomain:
    """A synthetic domain the mock owns, in the provider reaper ``OwnedDomain`` shape.

    A concrete dataclass that structurally satisfies the
    :class:`~kdive.providers.infra.reaping.OwnedDomain` protocol (``name`` + ``system_id``), so a
    :class:`FaultInjectReaper` plugs into leaked-domain repair directly.
    """

    name: str
    system_id: UUID | None


class FaultInjectInventory:
    """In-process registry of the synthetic domains the mock provider owns."""

    def __init__(self) -> None:
        self._domains: dict[str, UUID] = {}
        self._orphaned: set[str] = set()
        self._draws: dict[str, int] = {}
        self._lock = threading.Lock()

    def record(self, system_id: UUID, domain_name: str) -> None:
        with self._lock:
            self._domains[domain_name] = system_id
            self._draws.pop(domain_name, None)

    def forget(self, domain_name: str) -> None:
        with self._lock:
            self._domains.pop(domain_name, None)
            self._draws.pop(domain_name, None)
            self._orphaned.discard(domain_name)

    def next_draw(self, domain_name: str) -> tuple[UUID, int] | None:
        """Claim the domain's next 1-based draw ordinal, or ``None`` for an unknown domain.

        The ordinal is the ``attempt`` of a per-call fault draw (ADR-0072, ADR-0074). It lives with
        the domain, so every binding of one System shares it and a (re)provision or teardown
        restarts it.
        """
        with self._lock:
            system_id = self._domains.get(domain_name)
            if system_id is None:
                return None
            ordinal = self._draws.get(domain_name, 0) + 1
            self._draws[domain_name] = ordinal
            return system_id, ordinal

    def flag_orphan(self, domain_name: str) -> None:
        """Mark a domain as intentionally orphaned by a mid-op cancel (idempotent).

        The ``ORPHAN_FLAGGED`` cancel policy (``cancel_policy.py``) leaves the domain in the
        inventory rather than rolling it back; the flag records that the residue was left
        *deliberately* by a cancel, so a reaper/operator can tell it apart from best-effort
        tolerated residue. The entry itself is what the leaked-domain reconciler pass reaps. A
        domain already forgotten has no entry for the reaper to find, so no flag is stored.
        """
        with self._lock:
            if domain_name in self._domains:
                self._orphaned.add(domain_name)

    def is_orphaned(self, domain_name: str) -> bool:
        with self._lock:
            return domain_name in self._orphaned

    def system_id_of(self, domain_name: str) -> UUID | None:
        with self._lock:
            return self._domains.get(domain_name)

    def owned_domains(self) -> list[OwnedDomain]:
        with self._lock:
            return [OwnedDomain(name=name, system_id=sid) for name, sid in self._domains.items()]


class FaultInjectReaper:
    """The reconciler ``InfraReaper`` port backed by a :class:`FaultInjectInventory`."""

    def __init__(self, inventory: FaultInjectInventory) -> None:
        self._inventory = inventory

    async def list_owned(self) -> list[ReaperOwnedDomain]:
        return list(self._inventory.owned_domains())

    async def destroy(self, name: str) -> None:
        self._inventory.forget(name)
