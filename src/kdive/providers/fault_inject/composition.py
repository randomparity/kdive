"""Fault-inject provider runtime composition."""

from __future__ import annotations

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
    DebugCapabilities,
    ProviderRuntime,
    ProviderSupport,
)
from kdive.providers.fault_inject.debug.gdb import (
    FaultInjectDebugEngine,
    fault_inject_attach_seam,
)
from kdive.providers.fault_inject.debug.introspect import FaultInjectIntrospect
from kdive.providers.fault_inject.discovery import FaultInjectDiscovery
from kdive.providers.fault_inject.faulting.engine import FaultEngine
from kdive.providers.fault_inject.inventory import FaultInjectInventory, FaultInjectReaper
from kdive.providers.fault_inject.lifecycle.connect import FaultInjectConnect
from kdive.providers.fault_inject.lifecycle.control import FaultInjectControl
from kdive.providers.fault_inject.lifecycle.external_boot import FaultInjectExternalBoot
from kdive.providers.fault_inject.lifecycle.faulted import FaultedInstall, FaultedProvisioning
from kdive.providers.fault_inject.lifecycle.install import FaultInjectInstall
from kdive.providers.fault_inject.lifecycle.provisioning import FaultInjectProvisioning
from kdive.providers.fault_inject.profile_policy import FaultInjectProfilePolicy
from kdive.providers.fault_inject.retrieve import FaultInjectRetrieve
from kdive.providers.infra.reaping import InfraReaper
from kdive.store.assembly import UNCONFIGURED_OBJECT_STORE
from kdive.store.objectstore import ObjectStore

_POOL = "fault-inject"
# Synthetic provider cost reuses seeded `local`; unseeded classes fail closed in accounting.
_COST_CLASS = "local"


def _component_sources() -> ComponentSourceCapabilities:
    # ROOTFS only, mirroring local-libvirt: it is the one kind a caller can supply, and the one
    # kind `reject_unsupported_component_source` is reached for (ADR-0563, #1942). The former
    # KERNEL, VMLINUX, CONFIG, PATCH and INITRD entries declared a rejection that never happened.
    # Re-declare a kind in the same change that adds the caller entry point and its enforcement
    # call site; the guard in `tests/providers/test_capability_parity.py` fails a declaration
    # without one.
    accepted: dict[ComponentKind, frozenset[ComponentSourceKind]] = {
        ROOTFS_COMPONENT: frozenset({"catalog", "local"}),
    }
    return ComponentSourceCapabilities(
        provider=ResourceKind.FAULT_INJECT.value,
        accepted_component_sources=accepted,
    )


def discovery_registration() -> ProviderDiscoveryRegistration:
    # Bind-only (creates=False): fault-inject has no host to enumerate, so its row exists only
    # when declared in systems.toml. reconcile_resources is the sole creator (ADR-0112 #393);
    # the config overlay supplies the vcpus/memory_mb #385 needed. Letting discovery also insert
    # would produce a second, sizing-less row that collides on the (kind, name) identity.
    return ProviderDiscoveryRegistration(
        target_factory=_discovery_target,
        kind=ResourceKind.FAULT_INJECT,
        pool_name=_POOL,
        cost_class=_COST_CLASS,
        creates=False,
    )


def _discovery_target() -> DiscoveryRegistrationTarget:
    discovery = FaultInjectDiscovery.from_env()
    return DiscoveryRegistrationTarget(discovery=discovery, resource_id=discovery.host_uri)


def build_reaper(inventory: FaultInjectInventory) -> InfraReaper:
    return FaultInjectReaper(inventory)


def build_runtime(
    *,
    store: ObjectStore = UNCONFIGURED_OBJECT_STORE,
    inventory: FaultInjectInventory | None = None,
    engine: FaultEngine | None = None,
) -> ProviderRuntime:
    """Build fault-inject mock provider ports (ADR-0072 happy path; ADR-0074 faults)."""
    inventory = inventory if inventory is not None else FaultInjectInventory()
    provisioner = FaultInjectProvisioning(inventory)
    install = FaultInjectInstall()
    retrieve = FaultInjectRetrieve(store_factory=lambda: store)
    introspect = FaultInjectIntrospect()
    faulted_install = FaultedInstall(install, engine) if engine is not None else install
    external_boot = FaultInjectExternalBoot()
    return ProviderRuntime(
        profile_policy=FaultInjectProfilePolicy(),
        provisioner=FaultedProvisioning(provisioner, engine) if engine is not None else provisioner,
        installer=faulted_install,
        booter=faulted_install,
        connector=FaultInjectConnect(),
        controller=FaultInjectControl(),
        retriever=retrieve,
        crash_postmortem=retrieve,
        vmcore_introspector=introspect,
        live_introspector=introspect,
        support=ProviderSupport(
            component_sources=_component_sources(),
            capture_methods=frozenset(
                {CaptureMethod.CONSOLE, CaptureMethod.HOST_DUMP, CaptureMethod.GDBSTUB}
            ),
            # ADR-0208: fault-inject reports its synthetic capability: both connector transports
            # and both introspection modes FaultInjectIntrospect realizes.
            debug_transports=frozenset({"gdbstub", "drgn-live"}),
            introspection=frozenset({"offline-vmcore", "live"}),
        ),
        debug=DebugCapabilities(
            attach_seam=fault_inject_attach_seam,
            engine=FaultInjectDebugEngine(),
        ),
        external_boot=external_boot,
        external_boot_preparation=external_boot,
    )
