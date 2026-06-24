"""Neutral provider runtime contract.

The dataclass in this module is the high-level MCP and worker provider seam. It imports only
provider port protocols and domain value types; concrete provider assembly stays in
``kdive.providers.assembly.composition``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from psycopg_pool import AsyncConnectionPool

from kdive.components.references import ComponentRef
from kdive.components.validation import ComponentSourceCapabilities
from kdive.domain.capture import CaptureMethod
from kdive.domain.operations.jobs import DestructiveJobKind
from kdive.images.planes.base import RootfsBuildPlane
from kdive.profiles.provisioning import ProvisioningProfile, RootfsSource
from kdive.providers.ports import (
    AttachSeam,
    Booter,
    Builder,
    Connector,
    ConsoleSnapshotter,
    Controller,
    CrashPostmortem,
    DebugTransportKind,
    GdbMiEngine,
    Installer,
    IntrospectionMode,
    LiveIntrospector,
    Provisioner,
    Retriever,
    VmcoreIntrospector,
)

type DiscoveryRegistrar = Callable[[AsyncConnectionPool], Awaitable[None]]
type BuildConfigValidator = Callable[[ComponentRef], None]
type RootfsValidator = Callable[[RootfsSource], None]
type StagedVolumeProbe = Callable[[list[str]], Awaitable[dict[str, str]]]


def _unconfigured_component_sources() -> ComponentSourceCapabilities:
    return ComponentSourceCapabilities(provider="unconfigured", accepted_component_sources={})


@dataclass(frozen=True, slots=True)
class DebugCapabilities:
    """Optional live-debug capability group for providers that support gdb/MI."""

    attach_seam: AttachSeam
    engine: GdbMiEngine


class ProfilePolicy(Protocol):
    """Provider-owned behavior derived from a parsed provisioning profile."""

    def rootfs_source(self, profile: ProvisioningProfile) -> RootfsSource | None:
        """Return the rootfs source used by this provider, if any."""

    def ssh_credential_ref(self, profile: ProvisioningProfile) -> str | None:
        """Return the live-SSH credential reference used by this provider, if any."""

    def drgn_live_requires_credential(self, profile: ProvisioningProfile) -> bool:
        """Return whether drgn-live needs a profile credential."""

    def validate_profile(self, profile: ProvisioningProfile) -> None:
        """Run provider-specific static profile validation."""

    def destructive_opt_in(self, profile: ProvisioningProfile, op: DestructiveJobKind) -> bool:
        """Return whether the profile opts into a destructive operation."""

    def capture_method(self, profile: ProvisioningProfile) -> CaptureMethod:
        """Resolve the crash-capture method enabled by the profile."""

    def gdbstub_provisioned(self, profile: ProvisioningProfile) -> bool:
        """Return whether the System has a gdbstub endpoint (independent of capture_method)."""

    def host_dump_provisioned(self, profile: ProvisioningProfile) -> bool:
        """Return whether a host-side memory dump is available on a preserved crash."""


@dataclass(frozen=True, slots=True)
class ProviderRuntime:
    """Typed provider ports for the active runtime."""

    profile_policy: ProfilePolicy
    provisioner: Provisioner
    builder: Builder
    installer: Installer
    booter: Booter
    connector: Connector
    controller: Controller
    retriever: Retriever
    crash_postmortem: CrashPostmortem
    vmcore_introspector: VmcoreIntrospector
    live_introspector: LiveIntrospector
    # The provider capability descriptor (ADR-0208): three sibling frozensets read by the surface
    # (resources.describe) and capability-aware admission to answer "what can this provider do?".
    # Each defaults to **empty** (fail-closed): an unconfigured or partially-wired provider
    # advertises *no* capability, so the surface can never report a stubbed plane as working. A
    # plane joins its set only in the change that wires its real seam. ``supported_capture_methods``
    # is the authority for which core-producing methods ``vmcore.fetch`` admits; the per-System
    # default method is owned by ``ProfilePolicy.capture_method`` (ADR-0209), not duplicated here.
    supported_capture_methods: frozenset[CaptureMethod] = frozenset()
    supported_debug_transports: frozenset[DebugTransportKind] = frozenset()
    supported_introspection: frozenset[IntrospectionMode] = frozenset()
    # The platform-owned root device cmdline (ADR-0183). ``"root=/dev/vda"`` for direct-kernel
    # boot (local-libvirt's whole-disk-ext4 overlay); ``None`` when the in-guest bootloader owns
    # the root device (remote-libvirt inherits ``root=UUID=…`` via ``grubby --copy-default``).
    platform_root_cmdline: str | None = "root=/dev/vda"
    discovery_registrar: DiscoveryRegistrar | None = None
    debug: DebugCapabilities | None = None
    component_sources: ComponentSourceCapabilities = field(
        default_factory=_unconfigured_component_sources
    )
    build_config_validator: BuildConfigValidator | None = None
    rootfs_validator: RootfsValidator | None = None
    rootfs_build_plane: RootfsBuildPlane | None = None
    staged_volume_probe: StagedVolumeProbe | None = None
    # Per-Run console snapshot (ADR-0235). Set by providers whose console is captured out-of-band
    # (remote-libvirt: reconciler-resident collector → S3 parts); the boot worker invokes it to
    # persist an immutable ``console-<run>`` artifact. ``None`` → the boot handler captures the
    # worker-local console log directly (local-libvirt).
    console_snapshotter: ConsoleSnapshotter | None = None
    # Per-resource rebind hook (ADR-0187). A provider whose connection identity varies per
    # granted Resource (remote-libvirt: one inventory instance per host) sets this so the
    # resolver can bind the runtime's ports to the op's Resource by name. ``None`` → identity
    # (local-libvirt / fault-inject share one host, so no per-resource config).
    rebind_for_resource: Callable[[str], ProviderRuntime] | None = None

    async def register_discovery(self, pool: AsyncConnectionPool) -> None:
        if self.discovery_registrar is not None:
            await self.discovery_registrar(pool)

    def for_resource(self, resource_name: str) -> ProviderRuntime:
        """Return a runtime bound to ``resource_name``; identity when no rebind hook is set.

        The resolver calls this at the per-op chokepoint so a provider serving many hosts
        (remote-libvirt) resolves the *allocated* host's connection config, while single-host
        providers return themselves unchanged (ADR-0187).
        """
        if self.rebind_for_resource is None:
            return self
        return self.rebind_for_resource(resource_name)
