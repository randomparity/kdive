"""Remote-libvirt provider runtime composition."""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

import psycopg
from psycopg_pool import AsyncConnectionPool

from kdive.components.references import (
    CONFIG_COMPONENT,
    PATCH_COMPONENT,
    ComponentKind,
    ComponentSourceKind,
)
from kdive.components.validation import ComponentSourceCapabilities
from kdive.db.locks import CONSOLE_HOSTING_LEADER, SessionAdvisoryLock
from kdive.db.pool import create_pool, database_url
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.observability.console_telemetry import ConsoleTelemetry
from kdive.providers.core.discovery_registration import (
    DiscoveryRegistrationTarget,
    ProviderDiscoveryRegistration,
)
from kdive.providers.core.runtime import (
    ConsoleCapabilities,
    DebugCapabilities,
    ProviderRuntime,
    ProviderSupport,
    ResourceBindingCapabilities,
    ResourceDetailCapabilities,
    ResourceDetailProjector,
    RootfsCapabilities,
    StagedVolumeProbe,
)
from kdive.providers.core.transport_reset import TransportResetter
from kdive.providers.infra.console_hosting import (
    AsyncioPumpRunner,
    CollectorRegistry,
    ConsoleHosting,
    ConsoleHostingLoop,
    RunningSystems,
)
from kdive.providers.infra.reaping import DumpVolumeReaper
from kdive.providers.remote_libvirt.config import (
    RemoteLibvirtConfig,
    is_remote_libvirt_configured,
    remote_config_for_resource,
    unbound_remote_config,
)
from kdive.providers.remote_libvirt.connection.staged_volumes import probe_staged_volumes
from kdive.providers.remote_libvirt.connection.transport_reset import RemoteLibvirtTransportResetter
from kdive.providers.remote_libvirt.console.collector import ConsoleCollector, ConsoleStream
from kdive.providers.remote_libvirt.console.snapshot import RemoteLibvirtConsoleSnapshotter
from kdive.providers.remote_libvirt.console.wiring import (
    RemoteConsolePartStore,
    open_remote_console,
)
from kdive.providers.remote_libvirt.debug.gdbmi import remote_attach_seam
from kdive.providers.remote_libvirt.debug.introspect import (
    RemoteLibvirtLiveIntrospect,
    RemoteLibvirtVmcoreIntrospect,
)
from kdive.providers.remote_libvirt.lifecycle.connect import RemoteLibvirtConnect
from kdive.providers.remote_libvirt.lifecycle.control import RemoteLibvirtControl
from kdive.providers.remote_libvirt.lifecycle.install import RemoteLibvirtInstall
from kdive.providers.remote_libvirt.lifecycle.provisioning import RemoteLibvirtProvisioning
from kdive.providers.remote_libvirt.profile_policy import RemoteLibvirtProfilePolicy
from kdive.providers.remote_libvirt.reaping.dump_volume import RemoteLibvirtDumpVolumeReaper
from kdive.providers.remote_libvirt.resource_details import project_resource_details
from kdive.providers.remote_libvirt.retrieve.postmortem import CrashPostmortemAdapter
from kdive.providers.remote_libvirt.retrieve.retriever import RemoteLibvirtRetriever
from kdive.providers.remote_libvirt.rootfs_build import RemoteLibvirtRootfsBuildPlane
from kdive.providers.shared.debug_common.gdbmi.core.engine import GdbMiEngine
from kdive.providers.shared.debug_common.gdbmi.policy.debuginfo import (
    real_module_debuginfo_resolver,
)
from kdive.providers.shared.debug_common.gdbmi.policy.hostpolicy import allow_acl_remote
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env
from kdive.store.objectstore import object_store_from_env

_POOL = "remote-libvirt"
# Reuses seeded `local`; a remote seed row would be DDL beyond migration 0020.
_COST_CLASS = "local"
RunningSystemsFactory = Callable[[AsyncConnectionPool], RunningSystems]


def _component_sources() -> ComponentSourceCapabilities:
    # Remote-libvirt accepts catalog/local kernel config inputs and local patch artifacts for
    # uploaded-artifact workflows. No rootfs/kernel/initrd component source is accepted: the
    # target boots from an operator-staged disk-image base OS.
    accepted: dict[ComponentKind, frozenset[ComponentSourceKind]] = {
        CONFIG_COMPONENT: frozenset({"catalog", "local"}),
        PATCH_COMPONENT: frozenset({"local"}),
    }
    return ComponentSourceCapabilities(
        provider=ResourceKind.REMOTE_LIBVIRT.value,
        accepted_component_sources=accepted,
    )


def build_transport_resetter(*, secret_registry: SecretRegistry) -> TransportResetter:
    return RemoteLibvirtTransportResetter.from_env(secret_registry=secret_registry)


def build_dump_volume_reaper(*, secret_registry: SecretRegistry) -> DumpVolumeReaper:
    return RemoteLibvirtDumpVolumeReaper.from_env(secret_registry=secret_registry)


def resource_name_for_system(conninfo: str, system_id: UUID) -> str:
    """Resolve a remote System's bound resource (host) name: System→Allocation→Resource.

    The remote-libvirt resource ``name`` is its ``[[remote_libvirt]]`` instance name (ADR-0112),
    so this is the key the per-console open passes to :func:`remote_config_for_resource` to reach
    the System's *own* host in a multi-host fleet (ADR-0187, #395).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the System has no bound remote resource
            (no allocation row, or the join finds none) — the console cannot be opened without
            knowing which host owns the System.
    """
    with psycopg.connect(conninfo) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT r.name FROM systems s "
            "JOIN allocations a ON a.id = s.allocation_id "
            "JOIN resources r ON r.id = a.resource_id "
            "WHERE s.id = %s",
            (system_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise CategorizedError(
            f"system {system_id} has no bound remote-libvirt resource; cannot open its console",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return str(row[0])


def _open_console_for_system(
    system_id: UUID, *, conninfo: str, secret_backend: SecretBackend
) -> ConsoleStream:
    """Open ``system_id``'s console against the host config of the resource it is allocated to."""
    name = resource_name_for_system(conninfo, system_id)
    config = remote_config_for_resource(name)
    return open_remote_console(config, secret_backend, system_id)


async def build_console_hosting(
    *,
    secret_registry: SecretRegistry,
    running_systems_factory: RunningSystemsFactory,
    console_telemetry: ConsoleTelemetry | None = None,
) -> ConsoleHosting | None:
    """Build the single-leader remote console hosting loop, or ``None`` when unconfigured.

    The process-wide singletons (leader lock, pump runner, host pool) are bootstrapped once; the
    per-System console open resolves the System's *own* host config by its bound resource name, so
    one leader hosts consoles across a multi-host fleet (ADR-0187, #395).
    """
    if not is_remote_libvirt_configured():
        return None

    conninfo = database_url()
    store = object_store_from_env()
    secret_backend = secret_backend_from_env(registry=secret_registry)

    part_store = RemoteConsolePartStore(store, conninfo)
    leader_conn = await psycopg.AsyncConnection.connect(conninfo, autocommit=True)
    lock = SessionAdvisoryLock(leader_conn, CONSOLE_HOSTING_LEADER)
    runner = AsyncioPumpRunner()
    registry = CollectorRegistry(pump_runner=runner)
    host_pool = create_pool(min_size=1)
    await host_pool.open()

    def factory(system_id: object) -> ConsoleCollector:
        if not isinstance(system_id, UUID):
            raise TypeError("console collector factory expected a UUID system_id")
        return ConsoleCollector(
            system_id,
            open_console=lambda sid: _open_console_for_system(
                sid, conninfo=conninfo, secret_backend=secret_backend
            ),
            store=part_store,
            secret_registry=secret_registry,
            telemetry=console_telemetry,
        )

    loop = ConsoleHostingLoop(
        leader_lock=lock,
        running_systems=running_systems_factory(host_pool),
        collector_factory=factory,
        registry=registry,
        pump_runner=runner,
    )
    return ConsoleHosting(loop, registry, leader_conn, host_pool)


def discovery_registration(*, secret_registry: SecretRegistry) -> ProviderDiscoveryRegistration:
    # Remote-libvirt resource rows are created by reconcile_resources from the systems.toml overlay
    # (ADR-0112), never by discovery — the registration is creates=False, so the registrar is a
    # bind-only no-op and never resolves the target. The fleet is multi-host (ADR-0187), so there
    # is no single host to enumerate here; the target factory fails loudly if it is ever reached.
    del secret_registry
    return ProviderDiscoveryRegistration(
        target_factory=_no_discovery_target,
        kind=ResourceKind.REMOTE_LIBVIRT,
        pool_name=_POOL,
        cost_class=_COST_CLASS,
        creates=False,
    )


def _no_discovery_target() -> DiscoveryRegistrationTarget:
    raise CategorizedError(
        "remote-libvirt discovery does not create resource rows (creates=False); the fleet is "
        "registered by reconcile_resources from systems.toml",
        category=ErrorCategory.CONFIGURATION_ERROR,
    )


def _debug_capabilities(secret_registry: SecretRegistry) -> DebugCapabilities:
    return DebugCapabilities(
        attach_seam=remote_attach_seam,
        engine=GdbMiEngine(
            redactor_factory=lambda: Redactor(registry=secret_registry),
            host_policy=allow_acl_remote,
            module_debuginfo_resolver=real_module_debuginfo_resolver(),
        ),
    )


def _staged_volume_probe(
    config_factory: Callable[[], RemoteLibvirtConfig],
) -> StagedVolumeProbe:
    return lambda volumes: probe_staged_volumes(volumes, config_factory=config_factory)


def _resource_detail_projector(
    config_factory: Callable[[], RemoteLibvirtConfig],
) -> ResourceDetailProjector:
    return lambda pool, viewer_projects: project_resource_details(
        pool,
        viewer_projects,
        staged_probe=_staged_volume_probe(config_factory),
    )


def _rebind_for_resource(secret_registry: SecretRegistry) -> Callable[[str], ProviderRuntime]:
    def rebind(resource_name: str) -> ProviderRuntime:
        return build_runtime(
            secret_registry=secret_registry,
            config_factory=lambda: remote_config_for_resource(resource_name),
        )

    return rebind


def build_runtime(
    *,
    secret_registry: SecretRegistry,
    config_factory: Callable[[], RemoteLibvirtConfig] = unbound_remote_config,
) -> ProviderRuntime:
    """Build remote-libvirt ports; buildable without operator config (ADR-0076).

    ``config_factory`` resolves the remote host's connection config. By default it is the
    unbound resolver, which raises if a per-op port is reached without binding: the resolver
    rebinds the runtime per granted Resource via ``rebind_for_resource`` so a per-op call reaches
    the *allocated* host (ADR-0187, #395). The ``vmcore_introspector`` port takes no remote
    config — it operates on a fetched vmcore, not the remote libvirt host.
    """
    installer = RemoteLibvirtInstall.from_env(
        secret_registry=secret_registry, config_factory=config_factory
    )
    retriever = RemoteLibvirtRetriever.from_env(
        secret_registry=secret_registry, config_factory=config_factory
    )
    crash_postmortem = CrashPostmortemAdapter(secret_registry=secret_registry)
    vmcore_introspector = RemoteLibvirtVmcoreIntrospect.from_env(secret_registry=secret_registry)
    live_introspector = RemoteLibvirtLiveIntrospect.from_env(
        secret_registry=secret_registry, config_factory=config_factory
    )

    return ProviderRuntime(
        profile_policy=RemoteLibvirtProfilePolicy(),
        provisioner=RemoteLibvirtProvisioning(
            secret_registry=secret_registry, config_factory=config_factory
        ),
        installer=installer,
        booter=installer,
        connector=RemoteLibvirtConnect.from_env(
            secret_registry=secret_registry, config_factory=config_factory
        ),
        controller=RemoteLibvirtControl.from_env(
            secret_registry=secret_registry, config_factory=config_factory
        ),
        retriever=retriever,
        crash_postmortem=crash_postmortem,
        vmcore_introspector=vmcore_introspector,
        live_introspector=live_introspector,
        support=ProviderSupport(
            component_sources=_component_sources(),
            capture_methods=frozenset(
                {
                    CaptureMethod.KDUMP,
                    CaptureMethod.HOST_DUMP,
                    CaptureMethod.GDBSTUB,
                    CaptureMethod.CONSOLE,
                }
            ),
            # ADR-0208: remote reports what it already implements — both live-debug transports
            # (gdbstub + drgn-live, ADR-0083/0085) and both introspection modes (the wired
            # RemoteLibvirtVmcoreIntrospect / RemoteLibvirtLiveIntrospect ports).
            debug_transports=frozenset({"gdbstub", "drgn-live"}),
            introspection=frozenset({"offline-vmcore", "live", "live-script"}),
        ),
        debug=_debug_capabilities(secret_registry),
        rootfs=RootfsCapabilities(build_plane=RemoteLibvirtRootfsBuildPlane.from_env()),
        resource_details=ResourceDetailCapabilities(
            staged_volume_probe=_staged_volume_probe(config_factory),
            projector=_resource_detail_projector(config_factory),
        ),
        # ADR-0235: the reconciler-resident collector streams the console to S3 parts; the boot
        # worker assembles them into an immutable per-Run `console-<run>` artifact so a later boot
        # of the same System never overwrites earlier crash→fix evidence. Builds its store lazily
        # (this composition stays buildable without S3 config, ADR-0076).
        console=ConsoleCapabilities(snapshotter=RemoteLibvirtConsoleSnapshotter()),
        # The remote base image is partitioned and boots via in-guest GRUB, which already carries
        # the correct root=UUID=… (inherited by the install helper's grubby --copy-default). The
        # platform must not inject a root device or it overrides that (ADR-0183, #587).
        platform_root_cmdline=None,
        binding=ResourceBindingCapabilities(
            rebind_for_resource=_rebind_for_resource(secret_registry)
        ),
    )
