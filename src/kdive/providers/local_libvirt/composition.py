"""Local-libvirt provider runtime composition."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from kdive.components.references import (
    CONFIG_COMPONENT,
    INITRD_COMPONENT,
    KERNEL_COMPONENT,
    PATCH_COMPONENT,
    ROOTFS_COMPONENT,
    VMLINUX_COMPONENT,
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
from kdive.providers.infra.reaping import InfraReaper
from kdive.providers.local_libvirt.config import local_guest_egress_for_resource
from kdive.providers.local_libvirt.debug.gdbmi import default_attach_seam
from kdive.providers.local_libvirt.debug.introspect import LocalLibvirtVmcoreIntrospect
from kdive.providers.local_libvirt.debug.live_introspect import LocalLibvirtLiveIntrospect
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from kdive.providers.local_libvirt.lifecycle.connect import LocalLibvirtConnect
from kdive.providers.local_libvirt.lifecycle.control import LocalLibvirtControl
from kdive.providers.local_libvirt.lifecycle.install import LocalLibvirtInstall
from kdive.providers.local_libvirt.lifecycle.provisioning import LocalLibvirtProvisioning
from kdive.providers.local_libvirt.lifecycle.rootfs.overlay_customize import (
    authorized_key_customizer,
)
from kdive.providers.local_libvirt.lifecycle.snapshot import LocalLibvirtSnapshotter
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy
from kdive.providers.local_libvirt.reaping import LibvirtInfraReaper
from kdive.providers.local_libvirt.retrieve import LocalLibvirtRetrieve
from kdive.providers.local_libvirt.rootfs_build import LocalLibvirtRootfsBuildPlane
from kdive.providers.shared.debug_common.gdbmi.core.engine import GdbMiEngine
from kdive.providers.shared.debug_common.gdbmi.policy.debuginfo import (
    real_module_debuginfo_resolver,
)
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry

_POOL = "local-libvirt"
_COST_CLASS = "local"


def _component_sources() -> ComponentSourceCapabilities:
    accepted: dict[ComponentKind, frozenset[ComponentSourceKind]] = {
        ROOTFS_COMPONENT: frozenset({"catalog", "local"}),
        KERNEL_COMPONENT: frozenset({"local"}),
        INITRD_COMPONENT: frozenset({"local"}),
        CONFIG_COMPONENT: frozenset({"catalog", "local"}),
        PATCH_COMPONENT: frozenset({"local"}),
        VMLINUX_COMPONENT: frozenset({"local"}),
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


def build_rootfs_build_plane(*, workspace: Path | None = None) -> LocalLibvirtRootfsBuildPlane:
    """Build the local-libvirt rootfs build plane; runs no tool and opens no connection.

    ``workspace`` overrides the default build/publish location (the ``build-fs --workspace``
    operator flag), so an image can be built under a user-writable path.
    """
    return LocalLibvirtRootfsBuildPlane.from_env(workspace=workspace)


def _rebind_for_resource(secret_registry: SecretRegistry) -> Callable[[str], ProviderRuntime]:
    """Per-Resource rebind factory (ADR-0187/0313), mirroring remote-libvirt's shape.

    Captures only ``secret_registry`` (not ``build_runtime``'s enclosing scope) so a long-lived
    runtime does not retain the built ports through a closure.
    """

    def rebind(resource_name: str) -> ProviderRuntime:
        return build_runtime(secret_registry=secret_registry, resource_name=resource_name)

    return rebind


def build_runtime(
    *, secret_registry: SecretRegistry, resource_name: str | None = None
) -> ProviderRuntime:
    """Build local-libvirt provider ports without opening live provider connections.

    ``resource_name`` (ADR-0313, #1031) binds the provisioner to a specific local Resource's
    operator ``guest_egress`` opt-in, resolved op-time from ``systems.toml``. The resolver
    chokepoint (``ProviderRuntime.for_resource`` → ``rebind_for_resource``) supplies it per op; a
    ``None`` (host-agnostic construction) keeps the secure default (``restrict=on``).
    """
    guest_egress = (
        local_guest_egress_for_resource(resource_name) if resource_name is not None else False
    )
    provisioner = LocalLibvirtProvisioning.from_env(guest_egress=guest_egress)
    install = LocalLibvirtInstall.from_env()
    connector = LocalLibvirtConnect.from_env()
    controller = LocalLibvirtControl.from_env()
    retrieve = LocalLibvirtRetrieve.from_env(secret_registry=secret_registry)
    vmcore_introspector = LocalLibvirtVmcoreIntrospect.from_env(secret_registry=secret_registry)
    live_introspector = LocalLibvirtLiveIntrospect.from_env(secret_registry=secret_registry)
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
        ),
        debug=DebugCapabilities(
            attach_seam=default_attach_seam,
            engine=GdbMiEngine(
                redactor_factory=lambda: Redactor(registry=secret_registry),
                module_debuginfo_resolver=real_module_debuginfo_resolver(),
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
            rebind_for_resource=_rebind_for_resource(secret_registry)
        ),
        # Internal RAM+disk/disk-only domain snapshots (ADR-0378, #1254). Matches
        # ``support.supports_snapshots``; a snapshot-incapable provider leaves both unset.
        snapshot=LocalLibvirtSnapshotter.from_env(),
    )
