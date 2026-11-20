"""Fault-before-delegate wrappers using durable attempt inputs (ADR-0074)."""

from __future__ import annotations

import time
from collections.abc import Callable
from uuid import UUID

from kdive.domain.errors import CategorizedError
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.fault_inject.faulting.engine import FaultDecision, FaultEngine, FaultPlane
from kdive.providers.fault_inject.inventory import FaultInjectInventory
from kdive.providers.fault_inject.lifecycle.hotplug import FaultInjectHotplug
from kdive.providers.fault_inject.lifecycle.install import FaultInjectInstall
from kdive.providers.fault_inject.lifecycle.provisioning import FaultInjectProvisioning
from kdive.providers.ports.lifecycle import (
    DeviceSpec,
    HotplugDeviceKind,
    InstallRequest,
    PlugOutcome,
)
from kdive.serialization import JsonValue

_FIRST_ATTEMPT: Callable[[UUID], int] = lambda _system_id: 1  # noqa: E731 - a tiny default port
_SyncSleep = Callable[[float], None]
_AttemptFor = Callable[[UUID], int]


def _apply(decision: FaultDecision, sleep_s: _SyncSleep) -> None:
    """Apply latency before raising a drawn failure."""
    if decision.latency_s > 0.0:
        sleep_s(decision.latency_s)
    if decision.fail:
        if decision.category is None:
            raise RuntimeError("fault engine returned a failing decision without a category")
        raise CategorizedError(
            f"fault-inject drew a {decision.category.value} failure",
            category=decision.category,
        )


class FaultedProvisioning:
    """A :class:`FaultInjectProvisioning` that draws provision-plane faults before delegating."""

    def __init__(
        self,
        inner: FaultInjectProvisioning,
        engine: FaultEngine,
        *,
        attempt_for: _AttemptFor = _FIRST_ATTEMPT,
        sleep_s: _SyncSleep = time.sleep,
    ) -> None:
        self._inner = inner
        self._engine = engine
        self._attempt_for = attempt_for
        self._sleep_s = sleep_s

    def provision(
        self,
        system_id: UUID,
        profile: ProvisioningProfile,
        *,
        overlay_customizers: tuple[Callable[[str], None], ...] = (),
        bootstrap_pubkey: str | None = None,
        job_id: UUID | None = None,
    ) -> str:
        self._draw(system_id, FaultPlane.PROVISION)
        return self._inner.provision(
            system_id,
            profile,
            overlay_customizers=overlay_customizers,
            bootstrap_pubkey=bootstrap_pubkey,
            job_id=job_id,
        )

    def reprovision(
        self,
        system_id: UUID,
        profile: ProvisioningProfile,
        *,
        overlay_customizers: tuple[Callable[[str], None], ...] = (),
        bootstrap_pubkey: str | None = None,
        job_id: UUID | None = None,
    ) -> str:
        self._draw(system_id, FaultPlane.PROVISION)
        return self._inner.reprovision(
            system_id,
            profile,
            overlay_customizers=overlay_customizers,
            bootstrap_pubkey=bootstrap_pubkey,
            job_id=job_id,
        )

    def teardown(self, domain_name: str) -> None:
        # Teardown is compensation, not a perturbed op — it must always reap, so no draw.
        self._inner.teardown(domain_name)

    def read_resolved_cpu(self, system_id: UUID) -> dict[str, JsonValue] | None:
        """Return the wrapped provisioner's resolved CPU data."""
        return self._inner.read_resolved_cpu(system_id)

    def _draw(self, system_id: UUID, plane: FaultPlane) -> None:
        decision = self._engine.decide(
            system_id=system_id, plane=plane, attempt=self._attempt_for(system_id)
        )
        _apply(decision, self._sleep_s)


class FaultedInstall:
    """A :class:`FaultInjectInstall` that draws install/boot-plane faults before delegating."""

    def __init__(
        self,
        inner: FaultInjectInstall,
        engine: FaultEngine,
        *,
        attempt_for: _AttemptFor = _FIRST_ATTEMPT,
        sleep_s: _SyncSleep = time.sleep,
    ) -> None:
        self._inner = inner
        self._engine = engine
        self._attempt_for = attempt_for
        self._sleep_s = sleep_s

    def install(self, request: InstallRequest) -> None:
        self._draw(request.system_id, FaultPlane.INSTALL)
        self._inner.install(request)

    def boot(self, system_id: UUID, *, accel: str | None = None) -> None:
        self._draw(system_id, FaultPlane.BOOT)
        self._inner.boot(system_id, accel=accel)

    def _draw(self, system_id: UUID, plane: FaultPlane) -> None:
        decision = self._engine.decide(
            system_id=system_id, plane=plane, attempt=self._attempt_for(system_id)
        )
        _apply(decision, self._sleep_s)


class FaultedHotplug:
    """A :class:`FaultInjectHotplug` that draws hotplug-plane faults before plug and un-plug.

    ``is_attached`` and ``release_all`` are never perturbed: polling is how a settle is observed,
    and release is compensation that must always run. A domain the inventory does not know is
    delegated undrawn, so the inner port raises its own absent-domain error. Each draw takes the
    next per-System call ordinal from the inventory as ``attempt``, so successive calls on one
    System draw independently while staying a pure function of seed, System and call count.
    """

    def __init__(
        self,
        inner: FaultInjectHotplug,
        engine: FaultEngine,
        inventory: FaultInjectInventory,
        *,
        sleep_s: _SyncSleep = time.sleep,
    ) -> None:
        self._inner = inner
        self._engine = engine
        self._inventory = inventory
        self._sleep_s = sleep_s

    def plug(self, domain_name: str, spec: DeviceSpec) -> PlugOutcome:
        self._draw(domain_name)
        return self._inner.plug(domain_name, spec)

    def unplug(
        self,
        domain_name: str,
        device_id: str,
        provider_handle: str | None,
        *,
        kind: HotplugDeviceKind,
    ) -> bool:
        self._draw(domain_name)
        return self._inner.unplug(domain_name, device_id, provider_handle, kind=kind)

    def is_attached(
        self,
        domain_name: str,
        device_id: str,
        provider_handle: str | None,
        *,
        kind: HotplugDeviceKind,
    ) -> bool:
        return self._inner.is_attached(domain_name, device_id, provider_handle, kind=kind)

    def release_all(self, domain_name: str) -> None:
        self._inner.release_all(domain_name)

    def _draw(self, domain_name: str) -> None:
        draw = self._inventory.next_draw(domain_name)
        if draw is None:
            return
        system_id, attempt = draw
        decision = self._engine.decide(
            system_id=system_id, plane=FaultPlane.HOTPLUG, attempt=attempt
        )
        _apply(decision, self._sleep_s)
