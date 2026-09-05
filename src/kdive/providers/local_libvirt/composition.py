"""Local-libvirt provider runtime composition."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import UUID

import libvirt

import kdive.config as config
from kdive.components.references import (
    ROOTFS_COMPONENT,
    ComponentKind,
    ComponentSourceKind,
)
from kdive.components.validation import ComponentSourceCapabilities
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.resources import ResourceKind
from kdive.providers.core.discovery_registration import (
    DiscoveryRegistrationTarget,
    ProviderDiscoveryRegistration,
)
from kdive.providers.core.runtime import (
    BootstrapKeyCapabilities,
    DebugCapabilities,
    ProviderRuntime,
    ProviderSupport,
    ResourceBindingCapabilities,
    RootfsCapabilities,
)
from kdive.providers.external_boot_authority.host import (
    AuthorityHostConfig,
    validate_credential_paths,
)
from kdive.providers.infra.reaping import CaptureReaper, InfraReaper
from kdive.providers.local_libvirt.config import local_guest_egress_for_resource
from kdive.providers.local_libvirt.debug.gdbmi import default_attach_seam
from kdive.providers.local_libvirt.debug.introspect import LocalLibvirtVmcoreIntrospect
from kdive.providers.local_libvirt.debug.live_introspect import LocalLibvirtLiveIntrospect
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from kdive.providers.local_libvirt.lifecycle.boot.external_boot import (
    LocalExternalBootIO,
    LocalLibvirtExternalBoot,
)
from kdive.providers.local_libvirt.lifecycle.boot.session import (
    CleanupPayloads,
    Connect,
    LocalExternalBootSessionFactory,
    OpenArtifactRoot,
    OpenGuest,
    PinOperationLease,
    ReadinessProbe,
    RunningObserver,
)
from kdive.providers.local_libvirt.lifecycle.boot.session_mechanisms import (
    LocalArtifactRoot,
    LocalOperationLane,
    LocalPayloadCleanup,
    LocalRunningObserver,
    open_libguestfs_guest,
)
from kdive.providers.local_libvirt.lifecycle.capture_operation import (
    LocalLibvirtCaptureQuiescence,
)
from kdive.providers.local_libvirt.lifecycle.connect import LocalLibvirtConnect
from kdive.providers.local_libvirt.lifecycle.control import LocalLibvirtControl
from kdive.providers.local_libvirt.lifecycle.install import LocalLibvirtInstall
from kdive.providers.local_libvirt.lifecycle.provisioning import LocalLibvirtProvisioning
from kdive.providers.local_libvirt.lifecycle.rootfs.overlay_customize import (
    authorized_key_customizer,
)
from kdive.providers.local_libvirt.lifecycle.snapshot import LocalLibvirtSnapshotter
from kdive.providers.local_libvirt.lifecycle.traffic_capture import LocalLibvirtTrafficCapture
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy
from kdive.providers.local_libvirt.reaping import (
    LibvirtInfraReaper,
    LocalLibvirtCaptureReaper,
)
from kdive.providers.local_libvirt.retrieve.provider import LocalLibvirtRetrieve
from kdive.providers.local_libvirt.rootfs_build import LocalLibvirtRootfsBuildPlane
from kdive.providers.local_libvirt.settings import LIBVIRT_RECOVERY_ROOT, LIBVIRT_URI
from kdive.providers.ports.traffic import LocalCaptureConfiguration, TrafficCaptureOperationPorts
from kdive.providers.shared.debug_common.gdbmi.core.engine import GdbMiEngine
from kdive.providers.shared.debug_common.gdbmi.policy.debuginfo import (
    real_module_debuginfo_resolver,
)
from kdive.providers.shared.traffic_capture.execution import CaptureExecutor
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.assembly import UNCONFIGURED_OBJECT_STORE
from kdive.store.objectstore import ObjectStore

_POOL = "local-libvirt"
_COST_CLASS = "local"


def capture_operation_configuration(resource_id: UUID) -> bytes:
    """Snapshot the allowlisted local URI for post-filter child assembly."""
    return LocalCaptureConfiguration(
        resource_id=resource_id,
        uri=config.require(LIBVIRT_URI),
    ).to_canonical_json()


def build_capture_executor(
    configuration: LocalCaptureConfiguration,
) -> CaptureExecutor:
    """Reconstruct the local synchronous executor from released configuration."""
    import libvirt_qemu

    capturer = LocalLibvirtTrafficCapture(
        connect=lambda: libvirt.open(configuration.uri),
        monitor=libvirt_qemu.qemuMonitorCommand,
    )
    return CaptureExecutor(capturer=capturer, provider_label="local")


def build_capture_quiescence(
    configuration: LocalCaptureConfiguration,
) -> LocalLibvirtCaptureQuiescence:
    """Build an independent fresh-connection local absence probe."""
    import libvirt_qemu

    return LocalLibvirtCaptureQuiescence(
        resource_id=configuration.resource_id,
        connect=lambda: libvirt.open(configuration.uri),
        monitor=libvirt_qemu.qemuMonitorCommand,
    )


def _component_sources() -> ComponentSourceCapabilities:
    # ROOTFS only: it is the one kind a caller can supply, and the one kind
    # `reject_unsupported_component_source` is reached for (ADR-0563, #1942). A KERNEL, VMLINUX,
    # CONFIG, PATCH or INITRD entry here declared a rejection that never happened, because no
    # profile field or tool input carries a ref of those kinds — `ProvisioningProfile` carries
    # `rootfs` and nothing else, and `runs.kernel_ref` is written from build output. Re-declare a
    # kind in the same change that adds the caller entry point and its enforcement call site; the
    # guard in `tests/providers/test_capability_parity.py` fails a declaration without one.
    accepted: dict[ComponentKind, frozenset[ComponentSourceKind]] = {
        ROOTFS_COMPONENT: frozenset({"catalog", "local"}),
    }
    return ComponentSourceCapabilities(
        provider=ResourceKind.LOCAL_LIBVIRT.value,
        accepted_component_sources=accepted,
    )


def discovery_registration() -> ProviderDiscoveryRegistration:
    return ProviderDiscoveryRegistration(
        target_factory=_discovery_target,
        kind=ResourceKind.LOCAL_LIBVIRT,
        pool_name=_POOL,
        cost_class=_COST_CLASS,
    )


def _discovery_target() -> DiscoveryRegistrationTarget:
    discovery = LocalLibvirtDiscovery.from_env()
    return DiscoveryRegistrationTarget(discovery=discovery, resource_id=discovery.host_uri)


def build_reaper() -> InfraReaper:
    """Build the local-libvirt reconciler reaper (ADR-0111); opens no connection here."""
    return LibvirtInfraReaper.from_env()


def build_capture_reaper() -> CaptureReaper:
    """Build local-libvirt's orphaned-capture reaper (ADR-0556, ADR-0567, #1948).

    Detaches the job's QOM filter over the local ``KDIVE_LIBVIRT_URI`` connection and unlinks
    the job's pcap at the shared runtime-path convention. The reconciler for this kind is a
    root process colocated with the worker on the kdive host (ADR-0567's prerequisite), so it
    can reach both the hypervisor and the worker-owned path.
    """
    return LocalLibvirtCaptureReaper.from_env()


def build_rootfs_build_plane(*, workspace: Path | None = None) -> LocalLibvirtRootfsBuildPlane:
    """Build the local-libvirt rootfs build plane; runs no tool and opens no connection.

    ``workspace`` overrides the default build/publish location (the ``build-fs --workspace``
    operator flag), so an image can be built under a user-writable path.
    """
    return LocalLibvirtRootfsBuildPlane.from_env(workspace=workspace)


def build_external_boot_session_factory(
    *,
    pin_lease: PinOperationLease,
    open_artifact_root: OpenArtifactRoot,
    open_guest: OpenGuest,
    # Both widened to `| None` and deliberately left REQUIRED, with no default. The factory's own
    # `or _unconfigured_*` fallbacks then select the fail-closed defaults. Giving either a
    # default would make every mechanism omittable, so a caller that forgot `open_guest` or
    # `cleanup_payloads` would get a factory that looks built and fails only mid-operation.
    readiness: ReadinessProbe | None,
    observe_running: RunningObserver | None,
    cleanup_payloads: CleanupPayloads,
) -> LocalExternalBootSessionFactory:
    """Build the internal operation-session factory without opening host resources."""
    uri = config.require(LIBVIRT_URI)
    return LocalExternalBootSessionFactory(
        pin_lease=pin_lease,
        connect=cast(Connect, lambda: libvirt.open(uri)),
        open_artifact_root=open_artifact_root,
        open_guest=open_guest,
        readiness=readiness,
        observe_running=observe_running,
        cleanup_payloads=cleanup_payloads,
    )


@dataclass(frozen=True, slots=True)
class LocalExternalBootMechanisms:
    """The built factory beside the one recovery root every mechanism in it resolved.

    `recovery_root` is returned rather than left implicit because cleanup's archive removal
    and `RecoveryMetadataStore` must resolve the *same* root: a divergence would make cleanup
    open a non-existent path, report success under the idempotence rule, and leave
    `finalize_tombstone` failing for every archived activation. #2212 passes this value to the
    store rather than re-resolving the setting.
    """

    factory: LocalExternalBootSessionFactory
    recovery_root: Path


def build_external_boot_session_mechanisms() -> LocalExternalBootMechanisms:
    """Assemble the local external-boot host mechanisms (ADR-0591); opens nothing here.

    Takes no parameters: the only path into these mechanisms is the composition seam, so no
    caller can inject a root, URI, path, command or credential into them.
    """
    root = config.require(LIBVIRT_RECOVERY_ROOT)
    factory = build_external_boot_session_factory(
        pin_lease=LocalOperationLane().pin,
        open_artifact_root=LocalArtifactRoot(root).open,
        open_guest=open_libguestfs_guest,
        # `readiness` (amendment 7): `_real_readiness` reads a console log whose only
        # truncation happens in `LocalLibvirtInstall`'s prepare, and `_ConcreteSession.start()`
        # truncates nothing. On the `prior_power == "running"` arm that reaches it, the source
        # boot's `kdive-ready` marker is still in the file, and `classify_console` scans only
        # the bytes *before* the marker — so a target-boot panic is invisible and the gate
        # reports success. A correct probe must anchor its window at `start()`, which is in
        # `session.py`. Owner #2212, which cannot ship a live port without one because
        # `_unconfigured_readiness` raises on first call.
        #
        readiness=None,
        observe_running=LocalRunningObserver(),
        cleanup_payloads=LocalPayloadCleanup(root).cleanup,
    )
    return LocalExternalBootMechanisms(factory=factory, recovery_root=root)


def external_boot_authority_is_configured() -> bool:
    """Report whether the authenticated authority service-host boundary is installed.

    ADR-0584 makes this a precondition of advertising external-boot v1: a provider that
    cannot place every commit point behind the authority does not advertise it. The
    boundary is the authority host configuration together with its validated credential
    paths, so a host missing either fails closed rather than advertising an unfenced
    provider.
    """
    try:
        validate_credential_paths(AuthorityHostConfig.from_environment())
    except Exception:  # noqa: BLE001 - any unmet precondition closes the gate
        return False
    return True


def build_external_boot(
    io: LocalExternalBootIO | None = None,
) -> LocalLibvirtExternalBoot | None:
    """Bind the external-boot port only behind the authenticated authority boundary.

    ``io`` carries the local provider primitives: the operation lease, artifact root,
    guest access, readiness probe and payload cleanup that ``RealLocalExternalBootIO``
    composes. Both halves of ADR-0584's precondition must hold — the boundary configured
    *and* the primitives installed — so an absent ``io`` and an unconfigured boundary each
    leave ``ProviderRuntime.external_boot`` as ``None``.
    """
    if io is None or not external_boot_authority_is_configured():
        return None
    return LocalLibvirtExternalBoot(io)


def _rebind_for_resource(
    secret_registry: SecretRegistry, store: ObjectStore
) -> Callable[[str], ProviderRuntime]:
    """Per-Resource rebind factory (ADR-0187/0313), mirroring remote-libvirt's shape.

    Captures only ``secret_registry`` (not ``build_runtime``'s enclosing scope) so a long-lived
    runtime does not retain the built ports through a closure.
    """

    def rebind(resource_name: str) -> ProviderRuntime:
        return build_runtime(
            secret_registry=secret_registry, store=store, resource_name=resource_name
        )

    return rebind


def build_runtime(
    *,
    secret_registry: SecretRegistry,
    store: ObjectStore = UNCONFIGURED_OBJECT_STORE,
    resource_name: str | None = None,
    external_boot_io: LocalExternalBootIO | None = None,
) -> ProviderRuntime:
    """Build local-libvirt provider ports without opening live provider connections.

    ``store`` is the process-assembled object store captured by each object-store-aware port.
    ``resource_name`` (ADR-0313, #1031) binds the provisioner to a specific local Resource's
    operator ``guest_egress`` opt-in, resolved op-time from ``systems.toml``. The resolver
    chokepoint (``ProviderRuntime.for_resource`` → ``rebind_for_resource``) supplies it per op; a
    ``None`` (host-agnostic construction) keeps the secure default (``restrict=on``).
    ``external_boot_io`` supplies the local external-boot primitives; external boot is
    advertised only when they are present *and* the authenticated authority service-host
    boundary is configured (ADR-0584, #2199).
    """
    guest_egress = (
        local_guest_egress_for_resource(resource_name) if resource_name is not None else False
    )
    provisioner = LocalLibvirtProvisioning.from_env(store=store, guest_egress=guest_egress)
    install = LocalLibvirtInstall.from_env(store=store)
    connector = LocalLibvirtConnect.from_env()
    controller = LocalLibvirtControl.from_env()
    traffic_capturer = LocalLibvirtTrafficCapture.from_env()
    retrieve = LocalLibvirtRetrieve.from_env(secret_registry=secret_registry, store=store)
    vmcore_introspector = LocalLibvirtVmcoreIntrospect.from_env(
        secret_registry=secret_registry, store=store
    )
    live_introspector = LocalLibvirtLiveIntrospect.from_env(secret_registry=secret_registry)
    external_boot = build_external_boot(external_boot_io)
    return ProviderRuntime(
        profile_policy=LocalLibvirtProfilePolicy(),
        provisioner=provisioner,
        installer=install,
        booter=install,
        connector=connector,
        controller=controller,
        retriever=retrieve,
        crash_postmortem=retrieve,
        vmcore_introspector=vmcore_introspector,
        live_introspector=live_introspector,
        external_boot=external_boot,
        external_boot_preparation=external_boot,
        # ADR-0208: advertise the core-producing capture methods local can actually fetch a vmcore
        # for — KDUMP (host-side overlay harvest, #115/ADR-0203), FADUMP (the pseries firmware-
        # assisted variant sharing that harvest, ADR-0349; host support is gated at admission), and
        # HOST_DUMP (libvirt domain core dump, B4/ADR-0211); both debug transports from the domain
        # — gdbstub (#675/ADR-0210) and drgn-live over a loopback-forwarded guest SSH port
        # (#697/ADR-0218); and both introspection modes — offline-vmcore (B2 #676/ADR-0210 §2) and
        # live (B3 #677/ADR-0219, drgn-live SSH-exec of the in-guest kdive-drgn helper). All these
        # planes were proven live end-to-end on real KVM by the B6 (#680) milestone verifier, so
        # `debug.*` and `introspect.run` tool maturity is `implemented` (ADR-0218 §6 / ADR-0219).
        support=ProviderSupport(
            component_sources=_component_sources(),
            capture_methods=frozenset(
                {CaptureMethod.KDUMP, CaptureMethod.FADUMP, CaptureMethod.HOST_DUMP}
            ),
            debug_transports=frozenset({"gdbstub", "drgn-live"}),
            introspection=frozenset({"offline-vmcore", "live", "live-script"}),
            # Internal libvirt snapshots are supported on the local host (ADR-0378, #1254).
            supports_snapshots=True,
            # Host-side pcap via QEMU filter-dump on the local guest netdev (ADR-0385, #1258).
            supports_traffic_capture=True,
            # Magic-SysRq injection over the libvirt Control port on the local guest (ADR-0285).
            supports_diagnostic_sysrq=True,
            # Out-of-band crash-signature watch on the local guest's serial console (ADR-0367).
            supports_crash_watch=True,
        ),
        debug=DebugCapabilities(
            attach_seam=default_attach_seam,
            engine=GdbMiEngine(
                redactor_factory=lambda: Redactor(registry=secret_registry),
                module_debuginfo_resolver=real_module_debuginfo_resolver(store),
            ),
        ),
        rootfs=RootfsCapabilities(
            validator=provisioner.validate_rootfs_ref,
            build_plane=LocalLibvirtRootfsBuildPlane.from_env(),
        ),
        # The per-System bootstrap key (ADR-0289, #963) is injected via virt-customize into the
        # local overlay only local-libvirt owns; other providers leave this unset.
        bootstrap_key=BootstrapKeyCapabilities(customizer=authorized_key_customizer),
        # Per-Resource rebind (ADR-0187/0313, #1031): bind the operator guest_egress opt-in for the
        # allocated Resource by name. Previously unset (identity) — local now resolves per op.
        binding=ResourceBindingCapabilities(
            rebind_for_resource=_rebind_for_resource(secret_registry, store)
        ),
        # Internal RAM+disk/disk-only domain snapshots (ADR-0378, #1254). Matches
        # ``support.supports_snapshots``; a snapshot-incapable provider leaves both unset.
        snapshot=LocalLibvirtSnapshotter.from_env(),
        # Host-side filter-dump traffic capture (ADR-0385, #1258). Matches
        # ``support.supports_traffic_capture``.
        traffic_capturer=traffic_capturer,
        traffic_capture_operation=TrafficCaptureOperationPorts(
            configuration=capture_operation_configuration,
            quiescence=lambda raw: build_capture_quiescence(
                LocalCaptureConfiguration.from_canonical_json(raw)
            ),
        ),
    )
