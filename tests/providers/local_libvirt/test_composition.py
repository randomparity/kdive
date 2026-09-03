"""Local-libvirt provider composition tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from kdive.components.references import ROOTFS_COMPONENT
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.resources import ResourceKind
from kdive.providers.local_libvirt import composition
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
    LocalExternalBootSessionFactory,
    OpenArtifactRoot,
    OpenGuest,
    PinOperationLease,
    ReadinessProbe,
    RunningObserver,
)
from kdive.providers.local_libvirt.lifecycle.connect import LocalLibvirtConnect
from kdive.providers.local_libvirt.lifecycle.control import LocalLibvirtControl
from kdive.providers.local_libvirt.lifecycle.install import LocalLibvirtInstall
from kdive.providers.local_libvirt.lifecycle.provisioning import LocalLibvirtProvisioning
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy
from kdive.providers.local_libvirt.reaping import LibvirtInfraReaper
from kdive.providers.local_libvirt.retrieve.provider import LocalLibvirtRetrieve
from kdive.providers.local_libvirt.rootfs_build import LocalLibvirtRootfsBuildPlane
from kdive.providers.shared.debug_common.gdbmi.core.engine import GdbMiEngine
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import ObjectStore


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


def test_external_boot_session_factory_builder_is_lazy_and_unadvertised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = composition.build_runtime(secret_registry=SecretRegistry())
    assert runtime.external_boot is None
    opened: list[str] = []
    monkeypatch.setattr(composition.config, "require", lambda _setting: "qemu:///session")
    monkeypatch.setattr(composition.libvirt, "open", lambda uri: opened.append(uri))
    pin_lease = cast(PinOperationLease, lambda _lease: opened.append("pin"))
    open_artifact_root = cast(OpenArtifactRoot, lambda _ownership: opened.append("artifact") or 41)
    open_guest = cast(OpenGuest, lambda: opened.append("guest"))
    readiness = cast(ReadinessProbe, lambda _system_id: opened.append("readiness"))
    observe_running = cast(RunningObserver, lambda _system_id: opened.append("observation"))
    cleanup_payloads = cast(CleanupPayloads, lambda _descriptor, _binding: opened.append("cleanup"))

    factory = composition.build_external_boot_session_factory(
        pin_lease=pin_lease,
        open_artifact_root=open_artifact_root,
        open_guest=open_guest,
        readiness=readiness,
        observe_running=observe_running,
        cleanup_payloads=cleanup_payloads,
    )

    assert isinstance(factory, LocalExternalBootSessionFactory)
    assert opened == []


def test_configuring_a_recovery_root_does_not_advertise_external_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #2210 configures and provisions the recovery root; #2212 alone constructs
    # RealLocalExternalBootIO and binds it. A configured root is the one input that could
    # plausibly open the composition advertisement gate (#2199), so configure a valid one
    # and require composition to stay unadvertised anyway. This fails the moment someone
    # binds external_boot off the setting, which is the reordering the criterion exists to
    # catch.
    root = tmp_path / "external-boot-recovery" / "1"
    root.parent.mkdir()
    # mkdir() + chmod(), never mkdir(mode=...): the mode argument is masked by the umask.
    root.parent.chmod(0o711)
    root.mkdir()
    root.chmod(0o700)
    monkeypatch.setenv("KDIVE_LIBVIRT_RECOVERY_ROOT", str(root))
    composition.config.reset()

    runtime = composition.build_runtime(secret_registry=SecretRegistry())

    # Presence before value. Without this line the assertion below would pass vacuously if
    # the attribute were ever renamed or removed, and a gate test that passes on nothing is
    # worse than no gate test: it reads as proof the gate is still closed. Both halves were
    # fault-proven: a stub binding fails the value assertion, and a renamed attribute fails
    # this one while the getattr form silently passes.
    assert hasattr(runtime, "external_boot")
    assert runtime.external_boot is None


def test_build_runtime_wires_local_ports_and_capabilities() -> None:
    registry = SecretRegistry()
    runtime = composition.build_runtime(secret_registry=registry)

    assert isinstance(runtime.profile_policy, LocalLibvirtProfilePolicy)
    assert isinstance(runtime.provisioner, LocalLibvirtProvisioning)
    assert isinstance(runtime.installer, LocalLibvirtInstall)
    assert isinstance(runtime.booter, LocalLibvirtInstall)
    assert isinstance(runtime.connector, LocalLibvirtConnect)
    assert isinstance(runtime.controller, LocalLibvirtControl)
    assert isinstance(runtime.retriever, LocalLibvirtRetrieve)
    assert isinstance(runtime.crash_postmortem, LocalLibvirtRetrieve)
    assert isinstance(runtime.vmcore_introspector, LocalLibvirtVmcoreIntrospect)
    assert isinstance(runtime.live_introspector, LocalLibvirtLiveIntrospect)
    assert runtime.rootfs is not None
    assert isinstance(runtime.rootfs.build_plane, LocalLibvirtRootfsBuildPlane)
    # ADR-0208/0210/0211/0218/0219/0349: local advertises the core-producing capture methods it can
    # fetch — KDUMP (overlay harvest), FADUMP (the pseries firmware-assisted variant sharing that
    # harvest; host support gated at admission), and HOST_DUMP (libvirt domain core dump, B4) — both
    # debug transports (gdbstub B1 #675, drgn-live-over-SSH #697/ADR-0218), and both introspection
    # modes: offline-vmcore (B2 #676) and live (B3 #677/ADR-0219, drgn-live SSH-exec of in-guest).
    assert runtime.support.capture_methods == frozenset(
        {CaptureMethod.KDUMP, CaptureMethod.FADUMP, CaptureMethod.HOST_DUMP}
    )
    assert runtime.support.debug_transports == frozenset({"gdbstub", "drgn-live"})
    assert runtime.support.introspection == frozenset({"offline-vmcore", "live", "live-script"})
    assert runtime.debug is not None
    assert isinstance(runtime.debug.engine, GdbMiEngine)
    # Direct-kernel boot: the platform owns the whole-disk root device (ADR-0183).
    assert runtime.platform_root_cmdline == "root=/dev/vda"
    assert runtime.support.component_sources.provider == ResourceKind.LOCAL_LIBVIRT.value
    # ROOTFS only — the one kind with a caller entry point and an enforcement call site
    # (ADR-0563, #1942). Equality, not containment: an added kind must fail here too.
    assert runtime.support.component_sources.accepted_component_sources == {
        ROOTFS_COMPONENT: frozenset({"catalog", "local"}),
    }
    assert runtime.rootfs is not None
    assert runtime.rootfs.validator is not None


def test_build_runtime_threads_store_to_the_provisioner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = cast(ObjectStore, object())
    seen: list[object] = []

    class CapturingProvisioning:
        @classmethod
        def from_env(
            cls, *, store: ObjectStore, guest_egress: bool = False
        ) -> LocalLibvirtProvisioning:
            del cls, guest_egress
            seen.append(store)
            return LocalLibvirtProvisioning(connect=cast(Any, lambda: object()))

    monkeypatch.setattr(composition, "LocalLibvirtProvisioning", CapturingProvisioning)

    composition.build_runtime(secret_registry=SecretRegistry(), store=store)

    assert seen == [store]


def test_build_runtime_threads_store_to_module_debuginfo_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = cast(ObjectStore, object())
    seen: list[ObjectStore] = []

    def fake_resolver(store_arg: ObjectStore) -> object:
        seen.append(store_arg)
        return object()

    monkeypatch.setattr(composition, "real_module_debuginfo_resolver", fake_resolver)

    composition.build_runtime(secret_registry=SecretRegistry(), store=store)

    assert len(seen) == 1
    assert seen[0] is store


def test_local_runtime_sets_rebind_for_resource() -> None:
    # ADR-0313/0187: local now carries a per-Resource rebind hook so the resolver binds the
    # operator's guest_egress opt-in to the allocated Resource by name (previously identity/no-op).
    runtime = composition.build_runtime(secret_registry=SecretRegistry())
    assert runtime.binding is not None
    assert runtime.binding.rebind_for_resource is not None


def test_rebind_threads_guest_egress_into_provisioner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ADR-0313: for_resource(name) must bind the operator's resolved guest_egress to the
    # provisioner it hands back; a name with no matching [[local_libvirt]] block resolves to the
    # secure default (egress off). The resolver is monkeypatched here (the real-loader path is
    # covered in test_egress_config.py; provisioner->restrict rendering in test_provisioning.py).
    monkeypatch.setattr(
        composition,
        "local_guest_egress_for_resource",
        lambda name: name == "egress-on",
    )
    runtime = composition.build_runtime(secret_registry=SecretRegistry())

    on = cast("LocalLibvirtProvisioning", runtime.for_resource("egress-on").provisioner)
    off = cast("LocalLibvirtProvisioning", runtime.for_resource("egress-off").provisioner)
    assert on._guest_egress is True
    assert off._guest_egress is False
    # The host-agnostic build (no resource bound) keeps the secure default.
    base = cast("LocalLibvirtProvisioning", runtime.provisioner)
    assert base._guest_egress is False


def test_build_runtime_threads_secret_registry_into_secret_aware_ports() -> None:
    registry = SecretRegistry()
    runtime = composition.build_runtime(secret_registry=registry)

    # The single caller-supplied registry must reach every secret-aware port, not be
    # dropped (which would silently disable redaction for that port). The runtime fields
    # are typed as ports (Protocols); narrow to the concrete impls to inspect the wiring.
    retriever = cast("LocalLibvirtRetrieve", runtime.retriever)
    vmcore_introspector = cast("LocalLibvirtVmcoreIntrospect", runtime.vmcore_introspector)
    live_introspector = cast("LocalLibvirtLiveIntrospect", runtime.live_introspector)
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


# --------------------------------------------------------------------------------------
# External-boot advertisement gate (ADR-0584, #2199 acceptance criterion 11)
# --------------------------------------------------------------------------------------


def _external_boot_io() -> LocalExternalBootIO:
    """A stand-in for the local primitives; the gate must never dereference it."""
    return cast(LocalExternalBootIO, object())


def test_authority_service_host_boundary_is_unconfigured_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    assert composition.external_boot_authority_is_configured() is False


def test_runtime_without_the_authority_boundary_leaves_external_boot_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    runtime = composition.build_runtime(
        secret_registry=SecretRegistry(), external_boot_io=_external_boot_io()
    )

    assert runtime.external_boot is None


def test_runtime_without_the_local_primitives_leaves_external_boot_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(composition, "external_boot_authority_is_configured", lambda: True)

    runtime = composition.build_runtime(secret_registry=SecretRegistry())

    assert runtime.external_boot is None


def test_runtime_advertises_external_boot_only_with_boundary_and_primitives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(composition, "external_boot_authority_is_configured", lambda: True)

    runtime = composition.build_runtime(
        secret_registry=SecretRegistry(), external_boot_io=_external_boot_io()
    )

    assert isinstance(runtime.external_boot, LocalLibvirtExternalBoot)


def test_build_external_boot_refuses_each_half_of_the_precondition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(composition, "external_boot_authority_is_configured", lambda: False)
    assert composition.build_external_boot(_external_boot_io()) is None

    monkeypatch.setattr(composition, "external_boot_authority_is_configured", lambda: True)
    assert composition.build_external_boot(None) is None
    assert isinstance(
        composition.build_external_boot(_external_boot_io()), LocalLibvirtExternalBoot
    )
