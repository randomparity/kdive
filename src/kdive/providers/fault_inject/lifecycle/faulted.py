"""Fault-before-delegate wrappers using durable attempt inputs (ADR-0074)."""

from __future__ import annotations

import time
from collections.abc import Callable
from uuid import UUID

from kdive.domain.errors import CategorizedError
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.fault_inject.faulting.engine import FaultDecision, FaultEngine, FaultPlane
from kdive.providers.fault_inject.lifecycle.install import FaultInjectInstall
from kdive.providers.fault_inject.lifecycle.provisioning import FaultInjectProvisioning
from kdive.providers.ports.lifecycle import InstallRequest
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
