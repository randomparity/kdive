"""Neutral provider runtime contract.

The dataclass in this module is the high-level MCP and worker provider seam. It imports only
provider port protocols and domain value types; concrete provider assembly stays in
``kdive.providers.assembly.composition``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from psycopg_pool import AsyncConnectionPool

from kdive.components.validation import ComponentSourceCapabilities
from kdive.domain.capture import CaptureMethod
from kdive.images.planes.base import RootfsBuildPlane
from kdive.profiles.provider_policy import ProfilePolicy
from kdive.profiles.provisioning import RootfsSource
from kdive.providers.ports.console import ConsoleSnapshotter
from kdive.providers.ports.debug import (
    AttachSeam,
    GdbMiEngine,
)
from kdive.providers.ports.lifecycle import (
    Booter,
    Connector,
    Controller,
    DebugTransportKind,
    Installer,
    IntrospectionMode,
    Provisioner,
    Snapshotter,
)
from kdive.providers.ports.retrieve import (
    CrashPostmortem,
    LiveIntrospector,
    Retriever,
    VmcoreIntrospector,
)
from kdive.providers.ports.traffic import TrafficCapturer
from kdive.serialization import JsonValue

type DiscoveryRegistrar = Callable[[AsyncConnectionPool], Awaitable[None]]
type RootfsValidator = Callable[[RootfsSource], None]
type StagedVolumeProbe = Callable[[list[str]], Awaitable[dict[str, str]]]
type ResourceDetailProjector = Callable[
    [AsyncConnectionPool, tuple[str, ...]], Awaitable[dict[str, JsonValue]]
]


def _unconfigured_component_sources() -> ComponentSourceCapabilities:
    return ComponentSourceCapabilities(provider="unconfigured", accepted_component_sources={})


@dataclass(frozen=True, slots=True)
class DebugCapabilities:
    """Optional live-debug capability group for providers that support gdb/MI."""

    attach_seam: AttachSeam
    engine: GdbMiEngine


@dataclass(frozen=True, slots=True)
class ProviderSupport:
    """Provider-advertised support metadata read by admission and surfaces."""

    component_sources: ComponentSourceCapabilities = field(
        default_factory=_unconfigured_component_sources
    )
    capture_methods: frozenset[CaptureMethod] = frozenset()
    debug_transports: frozenset[DebugTransportKind] = frozenset()
    introspection: frozenset[IntrospectionMode] = frozenset()
    # System snapshot/restore support (ADR-0378). A static provider property (no libvirt I/O),
    # read at admission and surfaced on ``systems.get`` so an agent can discover it before use.
    # Fail-closed default: a future bare-metal provider leaves it False.
    supports_snapshots: bool = False
    # Host-side network traffic capture support (ADR-0385). Static provider property, surfaced on
    # ``systems.get`` for discovery. Fail-closed: only local-libvirt advertises it today.
    supports_traffic_capture: bool = False


@dataclass(frozen=True, slots=True)
class RootfsCapabilities:
    """Rootfs validation and build support for providers that own those planes."""

    validator: RootfsValidator | None = None
    build_plane: RootfsBuildPlane | None = None


@dataclass(frozen=True, slots=True)
class ResourceDetailCapabilities:
    """Inventory-detail projection and remote staged-volume probing support."""

    projector: ResourceDetailProjector | None = None
    staged_volume_probe: StagedVolumeProbe | None = None


@dataclass(frozen=True, slots=True)
class ConsoleCapabilities:
    """Provider-managed console artifact capture support."""

    snapshotter: ConsoleSnapshotter


@dataclass(frozen=True, slots=True)
class BootstrapKeyCapabilities:
    """System bootstrap-key overlay customization support."""

    customizer: Callable[[str], Callable[[str], None]]


@dataclass(frozen=True, slots=True)
class ResourceBindingCapabilities:
    """Per-resource runtime rebinding support for multi-resource providers."""

    rebind_for_resource: Callable[[str], ProviderRuntime]


@dataclass(frozen=True, slots=True)
class ProviderRuntime:
    """Typed provider ports for the active runtime."""

    profile_policy: ProfilePolicy
    provisioner: Provisioner
    installer: Installer
    booter: Booter
    connector: Connector
    controller: Controller
    retriever: Retriever
    crash_postmortem: CrashPostmortem
    vmcore_introspector: VmcoreIntrospector
    live_introspector: LiveIntrospector
    # The platform-owned root device cmdline (ADR-0183). ``"root=/dev/vda"`` for direct-kernel
    # boot (local-libvirt's whole-disk-ext4 overlay); ``None`` when the in-guest bootloader owns
    # the root device (remote-libvirt inherits ``root=UUID=…`` via ``grubby --copy-default``).
    platform_root_cmdline: str | None = "root=/dev/vda"
    discovery_registrar: DiscoveryRegistrar | None = None
    # Provider-advertised support (ADR-0208). Defaults fail closed: an unconfigured or partially
    # wired provider advertises no capture, debug, introspection, or component-source capability.
    support: ProviderSupport = field(default_factory=ProviderSupport)
    debug: DebugCapabilities | None = None
    rootfs: RootfsCapabilities | None = None
    resource_details: ResourceDetailCapabilities | None = None
    console: ConsoleCapabilities | None = None
    bootstrap_key: BootstrapKeyCapabilities | None = None
    binding: ResourceBindingCapabilities | None = None
    # System snapshot port (ADR-0378); ``None`` when the provider does not support snapshots
    # (kept consistent with ``support.supports_snapshots is False``).
    snapshot: Snapshotter | None = None
    # Host-side traffic capture port (ADR-0385); ``None`` when unsupported (kept consistent with
    # ``support.supports_traffic_capture is False``).
    traffic_capturer: TrafficCapturer | None = None

    async def register_discovery(self, pool: AsyncConnectionPool) -> None:
        if self.discovery_registrar is not None:
            await self.discovery_registrar(pool)

    def for_resource(self, resource_name: str) -> ProviderRuntime:
        """Return a runtime bound to ``resource_name``; identity when no rebind hook is set.

        The resolver calls this at the per-op chokepoint so a provider serving many hosts
        (remote-libvirt) resolves the *allocated* host's connection config, while single-host
        providers return themselves unchanged (ADR-0187).
        """
        if self.binding is None:
            return self
        return self.binding.rebind_for_resource(resource_name)
