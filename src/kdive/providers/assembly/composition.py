"""Active provider composition boundary.

This module owns the deployment opt-in table and aggregates provider-owned composition
factories into a ``ProviderResolver`` plus reconciler support ports. Provider-specific
runtime assembly lives next to each provider.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.config.core_settings import FAULT_INJECT, LOCAL_LIBVIRT_ENABLED
from kdive.db.build_hosts import BuildHostKind
from kdive.db.resource_discovery import ensure_discovered_resource_registered
from kdive.domain.catalog.resources import ResourceKind
from kdive.images.planes.base import RootfsBuildPlane
from kdive.observability.console_telemetry import ConsoleTelemetry
from kdive.providers.core.discovery_registration import ProviderDiscoveryRegistration
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.core.runtime import DiscoveryRegistrar, ProviderRuntime
from kdive.providers.core.transport_reset import NullResetter, TransportResetter
from kdive.providers.fault_inject import composition as fault_inject_composition
from kdive.providers.fault_inject.faulting.engine import FaultEngine
from kdive.providers.fault_inject.inventory import FaultInjectInventory
from kdive.providers.infra.console_hosting import DbRunningRemoteSystems
from kdive.providers.infra.reaping import (
    BuildVmReaper,
    DumpVolumeReaper,
    InfraReaper,
    NullBuildVmReaper,
    NullDumpVolumeReaper,
    NullReaper,
    OwnedDomain,
)
from kdive.providers.local_libvirt import composition as local_composition
from kdive.providers.remote_libvirt import composition as remote_composition
from kdive.providers.remote_libvirt.config import is_remote_libvirt_configured
from kdive.providers.shared.build_host.dispatch import BuildHostTransportFactory
from kdive.providers.shared.build_host.reachability import BuildHostProber, SshBuildHostProber
from kdive.security.secrets.secret_registry import SecretRegistry

if TYPE_CHECKING:
    from kdive.providers.infra.console_hosting import ConsoleHosting

type _ConsoleHostingFactory = Callable[[], Awaitable["ConsoleHosting | None"]]


@dataclass(frozen=True, slots=True)
class _RuntimeDescriptor:
    kind: ResourceKind
    enabled: Callable[[], bool]
    runtime_factory: Callable[[], ProviderRuntime]
    discovery_registration_factory: Callable[[], ProviderDiscoveryRegistration]

    def build_runtime(self) -> ProviderRuntime:
        return _with_discovery_registration(
            self.runtime_factory(), self.discovery_registration_factory()
        )


def _discovery_registrar(registration: ProviderDiscoveryRegistration) -> DiscoveryRegistrar:
    async def register(pool: AsyncConnectionPool) -> None:
        # A config-owned kind (creates=False) is bind-only: reconcile_resources is the sole
        # creator, so discovery must not insert a competing row (ADR-0112 #393).
        if not registration.creates:
            return
        # Known remote limitation: ensure_discovered_resource_registered calls
        # discovery.list_resources() synchronously inside its async transaction, and
        # remote TLS connect has no pre-connect timeout. Async offload is deferred.
        target = registration.target_factory()
        await ensure_discovered_resource_registered(
            pool,
            target.discovery,
            kind=registration.kind,
            resource_id=target.resource_id,
            pool_name=registration.pool_name,
            cost_class=registration.cost_class,
        )

    return register


def _with_discovery_registration(
    runtime: ProviderRuntime, registration: ProviderDiscoveryRegistration
) -> ProviderRuntime:
    return replace(runtime, discovery_registrar=_discovery_registrar(registration))


def build_local_runtime(*, secret_registry: SecretRegistry) -> ProviderRuntime:
    runtime = local_composition.build_runtime(secret_registry=secret_registry)
    return _with_discovery_registration(runtime, local_composition.discovery_registration())


def build_fault_inject_runtime(
    *, inventory: FaultInjectInventory | None = None, engine: FaultEngine | None = None
) -> ProviderRuntime:
    runtime = fault_inject_composition.build_runtime(inventory=inventory, engine=engine)
    return _with_discovery_registration(runtime, fault_inject_composition.discovery_registration())


def build_remote_runtime(*, secret_registry: SecretRegistry) -> ProviderRuntime:
    runtime = remote_composition.build_runtime(secret_registry=secret_registry)
    return _with_discovery_registration(
        runtime, remote_composition.discovery_registration(secret_registry=secret_registry)
    )


async def ensure_local_host_registered(pool: AsyncConnectionPool) -> None:
    await _discovery_registrar(local_composition.discovery_registration())(pool)


def build_local_rootfs_build_plane(*, workspace: Path | None = None) -> RootfsBuildPlane:
    """Build the local-libvirt rootfs build plane (the ``build-fs`` CLI seam).

    Routes the plane construction through this composition boundary so the CLI never imports a
    provider's internals directly; ``workspace`` overrides the default build/publish location.
    """
    return local_composition.build_rootfs_build_plane(workspace=workspace)


def _fault_inject_enabled(enable_fault_inject: bool | None) -> bool:
    """Resolve the opt-in gate: an explicit flag wins, else read the env (default off)."""
    if enable_fault_inject is not None:
        return enable_fault_inject
    return (config.get(FAULT_INJECT) or "").strip().lower() in {"1", "true", "yes"}


def _remote_libvirt_enabled(enable_remote_libvirt: bool | None) -> bool:
    """Resolve the opt-in gate: an explicit flag wins, else operator config presence."""
    if enable_remote_libvirt is not None:
        return enable_remote_libvirt
    return is_remote_libvirt_configured()


def _local_libvirt_enabled(enable_local_libvirt: bool | None) -> bool:
    """Resolve the gate: an explicit flag wins, else read the env (default on)."""
    if enable_local_libvirt is not None:
        return enable_local_libvirt
    return (config.get(LOCAL_LIBVIRT_ENABLED) or "").strip().lower() not in {"0", "false", "no"}


class _CompositeReaper:
    """Fan out leaked-domain listing, then route destroy to the provider that listed a domain."""

    def __init__(self, reapers: tuple[InfraReaper, ...]) -> None:
        self._reapers = reapers
        self._owners: dict[str, InfraReaper] = {}

    async def list_owned(self) -> list[OwnedDomain]:
        domains: list[OwnedDomain] = []
        owners: dict[str, InfraReaper] = {}
        for reaper in self._reapers:
            owned = await reaper.list_owned()
            domains.extend(owned)
            for domain in owned:
                owners.setdefault(domain.name, reaper)
        self._owners = owners
        return domains

    async def destroy(self, name: str) -> None:
        owner = self._owners.get(name)
        if owner is not None:
            await owner.destroy(name)


class ProviderComposition:
    """Own provider assembly state that must be shared across constructed ports."""

    def __init__(
        self,
        *,
        fault_inject_inventory: FaultInjectInventory | None = None,
        secret_registry: SecretRegistry | None = None,
    ) -> None:
        self._fault_inject_inventory = fault_inject_inventory or FaultInjectInventory()
        self._secret_registry = secret_registry or SecretRegistry()

    @property
    def secret_registry(self) -> SecretRegistry:
        """Return the redaction registry shared by provider-owned ports."""
        return self._secret_registry

    def _runtime_descriptors(
        self,
        *,
        enable_fault_inject: bool | None = None,
        enable_remote_libvirt: bool | None = None,
        enable_local_libvirt: bool | None = None,
    ) -> tuple[_RuntimeDescriptor, ...]:
        return (
            _RuntimeDescriptor(
                kind=ResourceKind.LOCAL_LIBVIRT,
                enabled=lambda: _local_libvirt_enabled(enable_local_libvirt),
                runtime_factory=lambda: local_composition.build_runtime(
                    secret_registry=self._secret_registry
                ),
                discovery_registration_factory=local_composition.discovery_registration,
            ),
            _RuntimeDescriptor(
                kind=ResourceKind.FAULT_INJECT,
                enabled=lambda: _fault_inject_enabled(enable_fault_inject),
                runtime_factory=lambda: fault_inject_composition.build_runtime(
                    inventory=self._fault_inject_inventory
                ),
                discovery_registration_factory=fault_inject_composition.discovery_registration,
            ),
            _RuntimeDescriptor(
                kind=ResourceKind.REMOTE_LIBVIRT,
                enabled=lambda: _remote_libvirt_enabled(enable_remote_libvirt),
                runtime_factory=lambda: remote_composition.build_runtime(
                    secret_registry=self._secret_registry
                ),
                discovery_registration_factory=lambda: remote_composition.discovery_registration(
                    secret_registry=self._secret_registry
                ),
            ),
        )

    def _enabled_runtime_descriptors(
        self,
        *,
        enable_fault_inject: bool | None = None,
        enable_remote_libvirt: bool | None = None,
        enable_local_libvirt: bool | None = None,
    ) -> tuple[_RuntimeDescriptor, ...]:
        return tuple(
            descriptor
            for descriptor in self._runtime_descriptors(
                enable_fault_inject=enable_fault_inject,
                enable_remote_libvirt=enable_remote_libvirt,
                enable_local_libvirt=enable_local_libvirt,
            )
            if descriptor.enabled()
        )

    def _reconciler_reaper_factories(
        self,
        *,
        enable_fault_inject: bool | None,
        enable_local_libvirt: bool | None,
        libvirt_reaper: InfraReaper | None,
    ) -> tuple[Callable[[], InfraReaper], ...]:
        factories: list[Callable[[], InfraReaper]] = []
        if _local_libvirt_enabled(enable_local_libvirt):
            factories.append(lambda: libvirt_reaper or local_composition.build_reaper())
        if _fault_inject_enabled(enable_fault_inject):
            factories.append(
                lambda: fault_inject_composition.build_reaper(self._fault_inject_inventory)
            )
        return tuple(factories)

    def _transport_resetter_factories(
        self, *, enable_remote_libvirt: bool | None
    ) -> tuple[Callable[[], TransportResetter], ...]:
        if not _remote_libvirt_enabled(enable_remote_libvirt):
            return ()
        return (
            lambda: remote_composition.build_transport_resetter(
                secret_registry=self._secret_registry
            ),
        )

    def _dump_volume_reaper_factories(
        self, *, enable_remote_libvirt: bool | None
    ) -> tuple[Callable[[], DumpVolumeReaper], ...]:
        if not _remote_libvirt_enabled(enable_remote_libvirt):
            return ()
        return (
            lambda: remote_composition.build_dump_volume_reaper(
                secret_registry=self._secret_registry
            ),
        )

    def _build_vm_reaper_factories(
        self, *, enable_remote_libvirt: bool | None
    ) -> tuple[Callable[[], BuildVmReaper], ...]:
        if not _remote_libvirt_enabled(enable_remote_libvirt):
            return ()
        return (
            lambda: remote_composition.build_build_vm_reaper(secret_registry=self._secret_registry),
        )

    def _build_host_transport_factory_maps(
        self, *, enable_remote_libvirt: bool | None
    ) -> tuple[Callable[[], Mapping[BuildHostKind, BuildHostTransportFactory]], ...]:
        if not _remote_libvirt_enabled(enable_remote_libvirt):
            return ()
        return (
            lambda: {
                BuildHostKind.EPHEMERAL_LIBVIRT: (
                    remote_composition.build_ephemeral_build_transport_factory(
                        secret_registry=self._secret_registry
                    )
                )
            },
        )

    def _console_hosting_factories(
        self,
        *,
        enable_remote_libvirt: bool | None,
        console_telemetry: ConsoleTelemetry | None = None,
    ) -> tuple[_ConsoleHostingFactory, ...]:
        if not _remote_libvirt_enabled(enable_remote_libvirt):
            return ()
        return (
            lambda: remote_composition.build_console_hosting(
                secret_registry=self._secret_registry,
                running_systems_factory=DbRunningRemoteSystems,
                console_telemetry=console_telemetry,
            ),
        )

    def build_provider_resolver(
        self,
        *,
        enable_fault_inject: bool | None = None,
        enable_remote_libvirt: bool | None = None,
        enable_local_libvirt: bool | None = None,
    ) -> ProviderResolver:
        """Assemble the per-deployment ``ResourceKind -> ProviderRuntime`` registry.

        ``enable_local_libvirt`` gates the local-libvirt runtime the same way the reaper is
        gated (ADR-0127/0131): when disabled, local-libvirt is not composed into the resolver,
        so ``register_all_discovery`` never runs its discovery registrar against a missing
        libvirt socket. An explicit flag wins, else ``KDIVE_LOCAL_LIBVIRT_ENABLED`` (default on).
        """
        runtimes = {
            descriptor.kind: descriptor.build_runtime()
            for descriptor in self._enabled_runtime_descriptors(
                enable_fault_inject=enable_fault_inject,
                enable_remote_libvirt=enable_remote_libvirt,
                enable_local_libvirt=enable_local_libvirt,
            )
        }
        return ProviderResolver(runtimes)

    def build_reconciler_reaper(
        self,
        *,
        enable_fault_inject: bool | None = None,
        enable_local_libvirt: bool | None = None,
        libvirt_reaper: InfraReaper | None = None,
    ) -> InfraReaper:
        """Assemble the provider-aware leaked-infra reaper for reconciliation.

        Local-libvirt is on by default (``KDIVE_LOCAL_LIBVIRT_ENABLED``), so a stock deployment
        composes the libvirt-backed reaper (ADR-0111) and an orphaned ``kdive-<uuid>`` domain
        reaches ``repair_leaked_domains``. A remote-libvirt-only deployment with no local libvirt
        socket (e.g. k8s) sets the flag false so the sweep does not fail every pass; the fault-
        inject reaper is composed in when enabled. With neither enabled the reaper is a
        :class:`NullReaper`. ``libvirt_reaper`` is an injection seam for tests (the real reaper
        opens a libvirt connection on ``list_owned``); production passes ``None``.
        """
        reapers = [
            factory()
            for factory in self._reconciler_reaper_factories(
                enable_fault_inject=enable_fault_inject,
                enable_local_libvirt=enable_local_libvirt,
                libvirt_reaper=libvirt_reaper,
            )
        ]
        if not reapers:
            return NullReaper()
        if len(reapers) == 1:
            return reapers[0]
        return _CompositeReaper(tuple(reapers))

    def build_reconciler_transport_resetter(
        self, *, enable_remote_libvirt: bool | None = None
    ) -> TransportResetter:
        """Assemble the reconciler's dead-session transport resetter (ADR-0086)."""
        for factory in self._transport_resetter_factories(
            enable_remote_libvirt=enable_remote_libvirt
        ):
            return factory()
        return NullResetter()

    def build_reconciler_dump_volume_reaper(
        self, *, enable_remote_libvirt: bool | None = None
    ) -> DumpVolumeReaper:
        """Assemble the reconciler's host_dump orphaned-volume reaper (ADR-0094)."""
        for factory in self._dump_volume_reaper_factories(
            enable_remote_libvirt=enable_remote_libvirt
        ):
            return factory()
        return NullDumpVolumeReaper()

    def build_reconciler_build_vm_reaper(
        self, *, enable_remote_libvirt: bool | None = None
    ) -> BuildVmReaper:
        """Assemble the reconciler's ephemeral build-VM reaper (ADR-0100)."""
        for factory in self._build_vm_reaper_factories(enable_remote_libvirt=enable_remote_libvirt):
            return factory()
        return NullBuildVmReaper()

    def build_build_host_transport_factories(
        self, *, enable_remote_libvirt: bool | None = None
    ) -> dict[BuildHostKind, BuildHostTransportFactory]:
        """Assemble provider-owned build-host transport factories."""
        factories: dict[BuildHostKind, BuildHostTransportFactory] = {}
        for factory_map in self._build_host_transport_factory_maps(
            enable_remote_libvirt=enable_remote_libvirt
        ):
            factories.update(factory_map())
        return factories

    def build_reconciler_build_host_prober(self) -> BuildHostProber:
        """Assemble the reconciler's SSH build-host reachability prober (ADR-0103).

        Wired unconditionally: SSH build hosts are independent of the remote-libvirt
        provider, so the prober is not gated on ``_remote_libvirt_enabled``. When no SSH
        hosts are registered the repair's query simply returns nothing.
        """
        return SshBuildHostProber(secret_registry=self._secret_registry)

    async def build_reconciler_console_hosting(
        self,
        *,
        enable_remote_libvirt: bool | None = None,
        console_telemetry: ConsoleTelemetry | None = None,
    ) -> ConsoleHosting | None:
        """Assemble provider-owned console hosting for the reconciler."""
        for factory in self._console_hosting_factories(
            enable_remote_libvirt=enable_remote_libvirt,
            console_telemetry=console_telemetry,
        ):
            return await factory()
        return None


__all__ = [
    "build_fault_inject_runtime",
    "build_local_runtime",
    "build_remote_runtime",
    "ensure_local_host_registered",
    "ProviderComposition",
]
