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
from kdive.domain.errors import CategorizedError
from kdive.profiles.provisioning import ProvisioningProfile
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
from kdive.providers.fault_inject.lifecycle.connect import FaultInjectConnect
from kdive.providers.fault_inject.lifecycle.control import FaultInjectControl
from kdive.providers.fault_inject.lifecycle.faulted import FaultedInstall, FaultedProvisioning
from kdive.providers.fault_inject.lifecycle.install import FaultInjectInstall
from kdive.providers.fault_inject.lifecycle.provisioning import FaultInjectProvisioning
from kdive.providers.fault_inject.profile_policy import FaultInjectProfilePolicy
from kdive.providers.fault_inject.retrieve import FaultInjectRetrieve
from kdive.providers.ports import InstallRequest


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
    assert runtime.debug is not None
    assert isinstance(runtime.debug.engine, FaultInjectDebugEngine)
    assert runtime.debug.attach_seam is fault_inject_attach_seam
    assert runtime.rootfs_validator is not None
    assert runtime.rootfs_validator(object()) is None
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

    output = builder.build(run_id, cast("object", object()))

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
