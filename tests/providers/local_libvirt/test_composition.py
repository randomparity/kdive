"""Local-libvirt provider composition tests."""

from __future__ import annotations

from typing import cast

from kdive.components.references import (
    CONFIG_COMPONENT,
    INITRD_COMPONENT,
    KERNEL_COMPONENT,
    PATCH_COMPONENT,
    ROOTFS_COMPONENT,
    VMLINUX_COMPONENT,
)
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.resources import ResourceKind
from kdive.providers.local_libvirt import composition
from kdive.providers.local_libvirt.build import LocalLibvirtBuild
from kdive.providers.local_libvirt.debug.gdbmi import default_attach_seam
from kdive.providers.local_libvirt.debug.introspect import (
    LocalLibvirtLiveIntrospect,
    LocalLibvirtVmcoreIntrospect,
)
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from kdive.providers.local_libvirt.lifecycle.connect import LocalLibvirtConnect
from kdive.providers.local_libvirt.lifecycle.control import LocalLibvirtControl
from kdive.providers.local_libvirt.lifecycle.install import LocalLibvirtInstall
from kdive.providers.local_libvirt.lifecycle.provisioning import LocalLibvirtProvisioning
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy
from kdive.providers.local_libvirt.reaping import LibvirtInfraReaper
from kdive.providers.local_libvirt.retrieve import LocalLibvirtRetrieve
from kdive.providers.local_libvirt.rootfs_build import LocalLibvirtRootfsBuildPlane
from kdive.providers.shared.debug_common.gdbmi import GdbMiEngine
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry


def test_discovery_registration_targets_local_libvirt() -> None:
    registration = composition.discovery_registration()
    target = registration.target_factory()

    assert registration.kind is ResourceKind.LOCAL_LIBVIRT
    assert registration.pool_name == "local-libvirt"
    assert registration.cost_class == "local"
    assert registration.creates is True
    assert isinstance(target.discovery, LocalLibvirtDiscovery)
    assert target.resource_id == target.discovery.host_uri


def test_build_reaper_is_local_libvirt_reaper() -> None:
    assert isinstance(composition.build_reaper(), LibvirtInfraReaper)


def test_build_runtime_wires_local_ports_and_capabilities() -> None:
    registry = SecretRegistry()
    runtime = composition.build_runtime(secret_registry=registry)

    assert isinstance(runtime.profile_policy, LocalLibvirtProfilePolicy)
    assert isinstance(runtime.provisioner, LocalLibvirtProvisioning)
    assert isinstance(runtime.builder, LocalLibvirtBuild)
    assert isinstance(runtime.installer, LocalLibvirtInstall)
    assert isinstance(runtime.booter, LocalLibvirtInstall)
    assert isinstance(runtime.connector, LocalLibvirtConnect)
    assert isinstance(runtime.controller, LocalLibvirtControl)
    assert isinstance(runtime.retriever, LocalLibvirtRetrieve)
    assert isinstance(runtime.crash_postmortem, LocalLibvirtRetrieve)
    assert isinstance(runtime.vmcore_introspector, LocalLibvirtVmcoreIntrospect)
    assert isinstance(runtime.live_introspector, LocalLibvirtLiveIntrospect)
    assert isinstance(runtime.rootfs_build_plane, LocalLibvirtRootfsBuildPlane)
    # ADR-0208/0210/0211/0218/0219: local advertises both core-producing capture methods it can
    # fetch — KDUMP (overlay harvest) and HOST_DUMP (libvirt domain core dump, B4) — both debug
    # transports (gdbstub B1 #675, drgn-live-over-SSH #697/ADR-0218), and both introspection modes:
    # offline-vmcore (B2 #676) and live (B3 #677/ADR-0219, drgn-live SSH-exec of in-guest helper).
    assert runtime.supported_capture_methods == frozenset(
        {CaptureMethod.KDUMP, CaptureMethod.HOST_DUMP}
    )
    assert runtime.supported_debug_transports == frozenset({"gdbstub", "drgn-live"})
    assert runtime.supported_introspection == frozenset({"offline-vmcore", "live"})
    assert runtime.debug is not None
    assert isinstance(runtime.debug.engine, GdbMiEngine)
    # Direct-kernel boot: the platform owns the whole-disk root device (ADR-0183).
    assert runtime.platform_root_cmdline == "root=/dev/vda"
    assert runtime.component_sources.provider == ResourceKind.LOCAL_LIBVIRT.value
    assert runtime.component_sources.accepted_component_sources == {
        ROOTFS_COMPONENT: frozenset({"catalog", "local"}),
        KERNEL_COMPONENT: frozenset({"local"}),
        INITRD_COMPONENT: frozenset({"local"}),
        CONFIG_COMPONENT: frozenset({"catalog", "local"}),
        PATCH_COMPONENT: frozenset({"local"}),
        VMLINUX_COMPONENT: frozenset({"local"}),
    }
    assert runtime.build_config_validator is not None
    assert runtime.rootfs_validator is not None


def test_build_runtime_threads_secret_registry_into_secret_aware_ports() -> None:
    registry = SecretRegistry()
    runtime = composition.build_runtime(secret_registry=registry)

    # The single caller-supplied registry must reach every secret-aware port, not be
    # dropped (which would silently disable redaction for that port). The runtime fields
    # are typed as ports (Protocols); narrow to the concrete impls to inspect the wiring.
    builder = cast("LocalLibvirtBuild", runtime.builder)
    retriever = cast("LocalLibvirtRetrieve", runtime.retriever)
    vmcore_introspector = cast("LocalLibvirtVmcoreIntrospect", runtime.vmcore_introspector)
    live_introspector = cast("LocalLibvirtLiveIntrospect", runtime.live_introspector)
    assert builder._secret_registry is registry
    assert retriever._secret_registry is registry
    assert vmcore_introspector._secret_registry is registry
    assert live_introspector._secret_registry is registry


def test_build_runtime_debug_uses_default_attach_seam() -> None:
    runtime = composition.build_runtime(secret_registry=SecretRegistry())
    assert runtime.debug is not None
    assert runtime.debug.attach_seam is default_attach_seam


def test_build_runtime_redactor_factory_masks_values_from_the_registry() -> None:
    registry = SecretRegistry()
    # Seed before composing: the factory's Redactor snapshots the registry it is given,
    # so a value registered now proves the factory was wired to THIS registry.
    registry.register("local-libvirt-capability-secret", scope=object())
    runtime = composition.build_runtime(secret_registry=registry)
    assert runtime.debug is not None

    # ty resolves the name `_redactor_factory` to the module-level helper of the same
    # name, masking the instance attribute set in GdbMiEngine.__init__; it exists at runtime.
    redactor = runtime.debug.engine._redactor_factory()  # ty: ignore[unresolved-attribute]
    assert isinstance(redactor, Redactor)
    masked = redactor.redact_text("prefix local-libvirt-capability-secret suffix")
    assert "local-libvirt-capability-secret" not in masked
