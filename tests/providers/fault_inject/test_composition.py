"""Fault-inject provider composition tests."""

from __future__ import annotations

import asyncio
from typing import cast
from uuid import uuid4

import pytest

from kdive.artifacts.storage import ArtifactWriteRequest, StoredArtifact
from kdive.build_artifacts.results import BuildOutput
from kdive.components.references import (
    CONFIG_COMPONENT,
    INITRD_COMPONENT,
    KERNEL_COMPONENT,
    PATCH_COMPONENT,
    ROOTFS_COMPONENT,
    VMLINUX_COMPONENT,
)
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import ServerBuildProfile
from kdive.profiles.provisioning import ProvisioningProfile, RootfsSource
from kdive.providers.fault_inject import composition
from kdive.providers.fault_inject._common import SYNTHETIC_BUILD_ID, TENANT
from kdive.providers.fault_inject.build import FaultInjectBuild
from kdive.providers.fault_inject.debug.gdb import (
    FaultInjectDebugEngine,
    fault_inject_attach_seam,
)
from kdive.providers.fault_inject.debug.introspect import FaultInjectIntrospect
from kdive.providers.fault_inject.discovery import FaultInjectDiscovery
from kdive.providers.fault_inject.faulting.engine import FaultEngine, FaultPlane
from kdive.providers.fault_inject.inventory import FaultInjectInventory, FaultInjectReaper
from kdive.providers.fault_inject.lifecycle.connect import FaultInjectConnect, synthetic_port
from kdive.providers.fault_inject.lifecycle.control import FaultInjectControl
from kdive.providers.fault_inject.lifecycle.faulted import FaultedInstall, FaultedProvisioning
from kdive.providers.fault_inject.lifecycle.install import FaultInjectInstall
from kdive.providers.fault_inject.lifecycle.provisioning import FaultInjectProvisioning
from kdive.providers.fault_inject.profile_policy import FaultInjectProfilePolicy
from kdive.providers.fault_inject.retrieve import FaultInjectRetrieve
from kdive.providers.ports import DebugTransportKind, InstallRequest, SystemHandle
from kdive.providers.ports.lifecycle import TransportHandleData
from kdive.security.artifacts.crash_commands import validate_crash_commands


def test_discovery_registration_is_bind_only_and_targets_synthetic_host() -> None:
    registration = composition.discovery_registration()
    target = registration.target_factory()

    assert registration.kind is ResourceKind.FAULT_INJECT
    assert registration.pool_name == "fault-inject"
    assert registration.cost_class == "local"
    assert registration.creates is False
    assert target.resource_id == "fault-inject://local"
    assert isinstance(target.discovery, FaultInjectDiscovery)
    assert target.discovery.host_uri == "fault-inject://local"


def test_build_reaper_wraps_the_supplied_inventory() -> None:
    inventory = FaultInjectInventory()
    domain = FaultInjectProvisioning(inventory).provision(
        uuid4(), profile=cast(ProvisioningProfile, object())
    )
    reaper = composition.build_reaper(inventory)

    assert isinstance(reaper, FaultInjectReaper)
    owned = asyncio.run(reaper.list_owned())
    assert [item.name for item in owned] == [domain]


def test_build_runtime_wires_fault_inject_ports_and_capabilities() -> None:
    runtime = composition.build_runtime(inventory=FaultInjectInventory())

    assert isinstance(runtime.profile_policy, FaultInjectProfilePolicy)
    assert isinstance(runtime.provisioner, FaultInjectProvisioning)
    assert isinstance(runtime.builder, FaultInjectBuild)
    assert isinstance(runtime.installer, FaultInjectInstall)
    assert isinstance(runtime.booter, FaultInjectInstall)
    assert isinstance(runtime.connector, FaultInjectConnect)
    assert isinstance(runtime.controller, FaultInjectControl)
    assert isinstance(runtime.retriever, FaultInjectRetrieve)
    assert isinstance(runtime.crash_postmortem, FaultInjectRetrieve)
    assert isinstance(runtime.vmcore_introspector, FaultInjectIntrospect)
    assert isinstance(runtime.live_introspector, FaultInjectIntrospect)
    assert runtime.supported_capture_methods == frozenset(
        {CaptureMethod.CONSOLE, CaptureMethod.HOST_DUMP, CaptureMethod.GDBSTUB}
    )
    # ADR-0208: fault-inject reports its synthetic capability — both transports its connector
    # accepts (gdbstub + drgn-live) and both introspection modes FaultInjectIntrospect realizes.
    assert runtime.supported_debug_transports == frozenset({"gdbstub", "drgn-live"})
    assert runtime.supported_introspection == frozenset({"offline-vmcore", "live"})
    assert runtime.debug is not None
    assert isinstance(runtime.debug.engine, FaultInjectDebugEngine)
    assert runtime.debug.attach_seam is fault_inject_attach_seam
    assert runtime.rootfs_validator is not None
    assert runtime.rootfs_validator(cast(RootfsSource, object())) is None
    assert runtime.component_sources.provider == ResourceKind.FAULT_INJECT.value
    assert runtime.component_sources.accepted_component_sources == {
        ROOTFS_COMPONENT: frozenset({"catalog", "local"}),
        KERNEL_COMPONENT: frozenset({"local"}),
        INITRD_COMPONENT: frozenset({"local"}),
        CONFIG_COMPONENT: frozenset({"local"}),
        PATCH_COMPONENT: frozenset({"local"}),
        VMLINUX_COMPONENT: frozenset({"local"}),
    }


def test_build_runtime_wires_the_supplied_inventory_into_the_provisioner() -> None:
    inventory = FaultInjectInventory()
    runtime = composition.build_runtime(inventory=inventory)
    provisioner = cast(FaultInjectProvisioning, runtime.provisioner)

    system_id = uuid4()
    domain = provisioner.provision(system_id, profile=cast(ProvisioningProfile, object()))

    owned = asyncio.run(composition.build_reaper(inventory).list_owned())
    assert [item.name for item in owned] == [domain]


def test_build_runtime_without_engine_uses_unwrapped_happy_path_ports() -> None:
    runtime = composition.build_runtime(inventory=FaultInjectInventory())

    assert not isinstance(runtime.provisioner, FaultedProvisioning)
    assert not isinstance(runtime.installer, FaultedInstall)
    assert not isinstance(runtime.booter, FaultedInstall)


def test_build_runtime_with_engine_wraps_provision_and_install_planes() -> None:
    inventory = FaultInjectInventory()
    engine = FaultEngine(
        seed=7,
        fault_rate={FaultPlane.PROVISION.value: 1.0, FaultPlane.INSTALL.value: 1.0},
        max_latency_s={},
    )
    runtime = composition.build_runtime(inventory=inventory, engine=engine)

    assert isinstance(runtime.provisioner, FaultedProvisioning)
    assert isinstance(runtime.installer, FaultedInstall)
    assert isinstance(runtime.booter, FaultedInstall)

    system_id = uuid4()
    with pytest.raises(CategorizedError):
        runtime.provisioner.provision(system_id, profile=cast(ProvisioningProfile, object()))


def test_build_runtime_engine_install_wrapper_draws_then_delegates() -> None:
    engine = FaultEngine(
        seed=7,
        fault_rate={FaultPlane.INSTALL.value: 0.0, FaultPlane.BOOT.value: 0.0},
        max_latency_s={},
    )
    runtime = composition.build_runtime(inventory=FaultInjectInventory(), engine=engine)

    system_id = uuid4()
    request = InstallRequest(
        system_id=system_id,
        run_id=uuid4(),
        kernel_ref="kernel-ref",
        cmdline="console=ttyS0",
    )

    assert runtime.installer.install(request) is None
    assert runtime.booter.boot(system_id) is None


def test_build_runtime_engine_wrapper_delegates_to_the_supplied_inventory() -> None:
    inventory = FaultInjectInventory()
    engine = FaultEngine(seed=7, fault_rate={FaultPlane.PROVISION.value: 0.0}, max_latency_s={})
    runtime = composition.build_runtime(inventory=inventory, engine=engine)

    system_id = uuid4()
    domain = runtime.provisioner.provision(system_id, profile=cast(ProvisioningProfile, object()))

    owned = asyncio.run(composition.build_reaper(inventory).list_owned())
    assert [item.name for item in owned] == [domain]


class _RecordingStore:
    """A StorePort double that records each write and returns a key-bearing result."""

    def __init__(self) -> None:
        self.requests: list[ArtifactWriteRequest] = []

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        self.requests.append(request)
        return StoredArtifact(
            key=request.key(),
            etag="etag",
            sensitivity=request.sensitivity,
            retention_class=request.retention_class,
        )


def test_build_stores_redacted_kernel_and_debuginfo_and_returns_their_refs() -> None:
    store = _RecordingStore()
    builder = FaultInjectBuild(store_factory=lambda: store)
    run_id = uuid4()

    output = builder.build(run_id, cast(ServerBuildProfile, object()))

    kernel_req, debuginfo_req = store.requests
    assert kernel_req.name == "kernel"
    assert kernel_req.data == b"fault-inject-kernel"
    assert debuginfo_req.name == "vmlinux"
    assert debuginfo_req.data == b"fault-inject-vmlinux"

    for req in store.requests:
        assert req.tenant == TENANT
        assert req.owner_kind == "runs"
        assert req.owner_id == str(run_id)
        assert req.sensitivity is Sensitivity.REDACTED
        assert req.retention_class == "kernel-build"

    assert isinstance(output, BuildOutput)
    assert output.kernel_ref == kernel_req.key()
    assert output.debuginfo_ref == debuginfo_req.key()
    assert output.kernel_ref != output.debuginfo_ref
    assert output.build_id == SYNTHETIC_BUILD_ID


def test_introspect_outputs_are_empty_but_non_null_collections() -> None:
    introspect = FaultInjectIntrospect()

    offline = introspect.from_vmcore(vmcore_ref="v", debuginfo_ref="d", expected_build_id="b")
    live = introspect.introspect_live(transport_handle="gdbstub://127.0.0.1:1234", helper="drgn")

    for output in (offline, live):
        assert output.tasks == {}
        assert output.modules == {}
        assert output.sysinfo == {}
        assert output.truncated is False


def test_synthetic_port_is_stable_and_within_the_documented_range() -> None:
    # "fi-38" hashes to a digest above the 64512-wide modulus and is byte-order
    # sensitive, so its exact port pins the modulus bounds and the big-endian read.
    port = synthetic_port("fi-38")

    assert port == 1089
    assert 1024 <= port <= 65535
    assert synthetic_port("fi-38") == port
    assert synthetic_port("fault-inject-other") != port


def test_open_transport_encodes_the_handle_derived_loopback_port() -> None:
    connect = FaultInjectConnect()
    system = SystemHandle("fault-inject-domain")

    handle = connect.open_transport(system, "gdbstub")

    decoded = TransportHandleData.decode(handle)
    assert decoded.kind == "gdbstub"
    assert decoded.host == "127.0.0.1"
    assert decoded.port == synthetic_port(str(system))


def test_open_transport_error_names_the_rejected_kind() -> None:
    connect = FaultInjectConnect()

    with pytest.raises(CategorizedError) as excinfo:
        connect.open_transport(
            SystemHandle("fault-inject-domain"), cast(DebugTransportKind, "bogus")
        )

    assert "bogus" in str(excinfo.value)


def test_attach_seam_attachment_carries_a_live_controller(tmp_path) -> None:
    attachment = fault_inject_attach_seam(
        host="127.0.0.1", port=1234, run_id="run", transcript_path=tmp_path / "s.log"
    )

    assert attachment.controller is not None
    assert attachment.rsp_host == "127.0.0.1"
    assert attachment.rsp_port == 1234


def test_debug_engine_breakpoints_number_sequentially_with_full_fields(tmp_path) -> None:
    engine = FaultInjectDebugEngine()
    attachment = fault_inject_attach_seam(
        host="127.0.0.1", port=1234, run_id="run", transcript_path=tmp_path / "s.log"
    )

    first = engine.set_breakpoint(attachment, "vfs_read")
    second = engine.set_breakpoint(attachment, "do_exit")

    assert first.number == "1"
    assert second.number == "2"
    assert first.type == "breakpoint"
    assert first.func == "vfs_read"
    assert first.enabled is True


def test_clear_breakpoint_removes_only_the_named_one(tmp_path) -> None:
    engine = FaultInjectDebugEngine()
    attachment = fault_inject_attach_seam(
        host="127.0.0.1", port=1234, run_id="run", transcript_path=tmp_path / "s.log"
    )
    first = engine.set_breakpoint(attachment, "vfs_read")
    second = engine.set_breakpoint(attachment, "do_exit")

    engine.clear_breakpoint(attachment, first.number)

    remaining = engine.list_breakpoints(attachment)
    assert [ref.number for ref in remaining] == [second.number]


def test_clear_breakpoint_is_a_noop_for_an_unknown_number(tmp_path) -> None:
    engine = FaultInjectDebugEngine()
    attachment = fault_inject_attach_seam(
        host="127.0.0.1", port=1234, run_id="run", transcript_path=tmp_path / "s.log"
    )
    kept = engine.set_breakpoint(attachment, "vfs_read")

    engine.clear_breakpoint(attachment, "999")

    remaining = engine.list_breakpoints(attachment)
    assert [ref.number for ref in remaining] == [kept.number]


def test_capture_writes_sensitive_and_redacted_vmcore_artifacts() -> None:
    store = _RecordingStore()
    retrieve = FaultInjectRetrieve(store_factory=lambda: store)
    system_id = uuid4()

    run_id = uuid4()
    output = retrieve.capture(system_id, run_id, CaptureMethod.HOST_DUMP)

    raw_req, redacted_req = store.requests
    assert raw_req.name == "vmcore-host_dump"
    assert raw_req.sensitivity is Sensitivity.SENSITIVE
    assert redacted_req.name == "vmcore-host_dump-redacted"
    assert redacted_req.sensitivity is Sensitivity.REDACTED

    for req in store.requests:
        assert req.tenant == TENANT
        assert req.owner_kind == "runs"
        assert req.owner_id == str(run_id)
        assert req.data == b"fault-inject-vmcore"
        assert req.retention_class == "vmcore"

    assert output.raw.key == raw_req.key()
    assert output.raw.sensitivity is Sensitivity.SENSITIVE
    assert output.redacted.key == redacted_req.key()
    assert output.redacted.sensitivity is Sensitivity.REDACTED
    assert output.vmcore_build_id == SYNTHETIC_BUILD_ID
    assert output.raw_size_bytes == len(b"fault-inject-vmcore")


def test_crash_postmortem_maps_each_command_to_a_synthetic_result() -> None:
    retrieve = FaultInjectRetrieve(store_factory=_RecordingStore)

    output = retrieve.run_crash_postmortem(
        vmcore_ref="v", debuginfo_ref="d", expected_build_id="b", commands=["bt", "ps"]
    )

    assert output.results == {"bt": "synthetic", "ps": "synthetic"}
    assert output.transcript == "fault-inject postmortem"
    assert output.truncated is False


def test_crash_postmortem_rejection_carries_the_reason_in_details() -> None:
    retrieve = FaultInjectRetrieve(store_factory=_RecordingStore)
    bad_command = "bt | sh"
    reason = validate_crash_commands([bad_command])
    assert reason is not None

    with pytest.raises(CategorizedError) as excinfo:
        retrieve.run_crash_postmortem(
            vmcore_ref="v", debuginfo_ref="d", expected_build_id="b", commands=[bad_command]
        )

    error = excinfo.value
    assert str(error) == "crash command batch rejected"
    assert error.category is ErrorCategory.CONFIGURATION_ERROR
    assert error.details == {"reason": reason}
