"""Fault-inject Provisioning plane."""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.fault_inject.inventory import FaultInjectInventory


def domain_name(system_id: UUID) -> str:
    return f"fault-inject-{system_id}"


class FaultInjectProvisioning:
    """Provisioner port: mint a synthetic domain and track it in the mock inventory."""

    def __init__(self, inventory: FaultInjectInventory) -> None:
        self._inventory = inventory

    def provision(
        self,
        system_id: UUID,
        profile: ProvisioningProfile,
        *,
        overlay_customizers: tuple[Callable[[str], None], ...] = (),
        bootstrap_pubkey: str | None = None,
    ) -> str:
        # No real overlay or guest exists for a synthetic domain (ADR-0289, #963; ADR-0291): the
        # customizers and bootstrap key accepted for Provisioner-call-site parity are never used.
        del profile, overlay_customizers, bootstrap_pubkey
        domain = domain_name(system_id)
        self._inventory.record(system_id, domain)
        return domain

    def teardown(self, domain_name: str) -> None:
        self._inventory.forget(domain_name)

    def reprovision(
        self,
        system_id: UUID,
        profile: ProvisioningProfile,
        *,
        overlay_customizers: tuple[Callable[[str], None], ...] = (),
        bootstrap_pubkey: str | None = None,
    ) -> str:
        self._inventory.forget(domain_name(system_id))
        return self.provision(
            system_id,
            profile,
            overlay_customizers=overlay_customizers,
            bootstrap_pubkey=bootstrap_pubkey,
        )
