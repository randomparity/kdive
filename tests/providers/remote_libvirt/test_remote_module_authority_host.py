"""Concrete remote authority-host module preparation boundary."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from kdive.providers.external_boot_authority.protocol import (
    AuthorityPreparationMutationRequestV1,
)
from kdive.providers.remote_libvirt.external_boot_authority import (
    DurableRemoteModuleVolumePreparationHost,
    RemoteModuleTerminalPreparationResponseV1,
    RemoteModuleVolumePreparationRequestV1,
    RemoteModuleVolumePreparationStore,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
    AttachmentInspection,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleOperationV1,
    RemoteModuleResultV1,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_operation import (
    RemoteModuleApplianceExecution,
    RemoteModuleVolumePreparation,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_preparation import (
    RemoteModulePreparationExecutor,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volumes import (
    BuiltSourceImage,
    ModuleTreeEntry,
    PreparedVolume,
    SourceFilesystemEvidence,
    render_module_volume_name,
)
from kdive.providers.remote_libvirt.remote_module_authority_host import (
    ConcreteRemoteModuleAuthorityHost,
    RemoteModuleAuthorityHostConfiguration,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.providers.remote_libvirt.lifecycle.rootfs.remote_module_appliance_support import (
    Clock,
    Executor,
)
from tests.providers.remote_libvirt.lifecycle.rootfs.remote_module_appliance_support import (
    Conn as ApplianceConn,
)
from tests.providers.remote_libvirt.lifecycle.rootfs.remote_module_appliance_support import (
    operation as module_operation,
)
from tests.providers.remote_libvirt.lifecycle.rootfs.remote_module_volumes_support import (
    Conn as StorageConn,
)
from tests.support.external_boot_plan import external_boot_plan


def _request() -> RemoteModuleVolumePreparationRequestV1:
    operation = module_operation()
    system_id = UUID(operation.system_id)
    run_id = UUID(operation.run_id)
    plan = external_boot_plan(system_id, run_id)
    operation = operation.model_copy(update={"plan_identity": plan.identity})
    authority = AuthorityPreparationMutationRequestV1(
        authority_id=uuid4(),
        generation=1,
        system_id=system_id,
        activation_id=uuid4(),
        run_id=run_id,
        plan_identity=plan.identity,
        purpose="activate",
        operation="prepare",
        provider_kind="remote-libvirt",
        authority_instance="remote-a",
        operation_identity="prepare-op",
        operation_digest="sha256:" + "c" * 64,
        attempt_id=uuid4(),
        expected_source_identity="source-a",
        intended_target_identity="target-a",
        recovery_objects=(),
        plan=plan,
    )
    return RemoteModuleVolumePreparationRequestV1(
        authority=authority, operation=operation, deadline=10_000.0
    )


class _Writer:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._evidence: SourceFilesystemEvidence | None = None

    def build(self, operation: bytes, entries: tuple[ModuleTreeEntry, ...]) -> BuiltSourceImage:
        self._path.write_bytes(b"source-image".ljust(4096, b"\0"))
        self._evidence = SourceFilesystemEvidence(
            operation=operation,
            manifest="sha256:" + "b" * 64,
            entry_count=len(entries),
            content_bytes=sum(len(entry.content or b"") for entry in entries),
        )
        return BuiltSourceImage(self._path, 4096, self._evidence)

    def inspect(self, path: Path) -> SourceFilesystemEvidence:
        assert path.read_bytes() == b"source-image".ljust(4096, b"\0")
        assert self._evidence is not None
        return self._evidence


class _Identity:
    def identity(self, path: str) -> None:
        return None


def _result(operation: RemoteModuleOperationV1) -> RemoteModuleResultV1:
    return RemoteModuleResultV1(
        status="success",
        phase="installed",
        system_id=operation.system_id,
        run_id=operation.run_id,
        plan_identity=operation.plan_identity,
        operation_nonce=operation.operation_nonce,
        appliance_image_digest=operation.appliance_image_digest,
        release=operation.release,
        root_volume_key=operation.root_volume.key,
        root_volume_identity=operation.root_volume.identity,
        source_manifest=operation.source_manifest,
        installed_manifest=operation.source_manifest,
        capture_absent=True,
        entry_count=1,
        content_bytes=3,
    )


@pytest.mark.anyio
async def test_concrete_host_creates_streams_runs_reopens_and_tears_down(tmp_path: Path) -> None:
    request = _request()
    operation = request.operation
    source_name = render_module_volume_name(
        operation.system_id, operation.run_id, operation.operation_nonce, "source.ext4"
    )
    scratch_name = render_module_volume_name(
        operation.system_id, operation.run_id, operation.operation_nonce, "scratch.ext4"
    )
    detached = AttachmentInspection(
        system_shut_off=True,
        exclusive=True,
        appliance_present=False,
        detached_volumes=frozenset({("systems", source_name), ("systems", scratch_name)}),
    )
    storage = StorageConn()
    clock = Clock()
    appliance = ApplianceConn([], clock)
    durable_result = _result(operation)
    volume_config = RemoteModuleVolumePreparation(
        storage=storage,
        pool_name="systems",
        entries=(ModuleTreeEntry("kernel.ko", 0o100644, content=b"abc"),),
        writer=_Writer(tmp_path / "source.ext4"),
        inspect_attachments=lambda identity_port, present_attempt_volumes=None: detached,
        work_dir=tmp_path,
    )
    appliance_config = RemoteModuleApplianceExecution(
        appliance=appliance,
        architecture="x86_64",
        emulator_path="/usr/bin/qemu-system-x86_64",
        memory_kib=262_144,
        vcpus=1,
        appliance_volume="appliance-x86_64.qcow2",
        appliance_image_digest=operation.appliance_image_digest,
        root=lambda _operation: PreparedVolume(
            "systems",
            operation.root_volume.key,
            operation.system_id,
            operation.run_id,
            operation.operation_nonce,
            "root",
            operation.root_volume.identity,
            4096,
        ),
        read_scratch_result=lambda _volume, _deadline: durable_result.to_wire_bytes(),
        inspect_attachments=lambda: detached,
        secret_registry=SecretRegistry(),
        deadline_executor=Executor(),
        monotonic=clock,
    )
    executor = RemoteModulePreparationExecutor()
    host = ConcreteRemoteModuleAuthorityHost(
        RemoteModuleAuthorityHostConfiguration(volume_config, appliance_config, _Identity()),
        executor,
    )

    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    store = RemoteModuleVolumePreparationStore(evidence_root)
    durable_host = DurableRemoteModuleVolumePreparationHost(store, host)
    response = await durable_host.execute(request)

    assert isinstance(response, RemoteModuleTerminalPreparationResponseV1)
    assert set(storage.pool.volumes) == {source_name, scratch_name}
    assert appliance.created_flags is not None
    assert appliance.domain is not None and appliance.domain.destroyed
    assert response.result == durable_result
    assert response.recovery.source_volume.ref == source_name
    response.validate_terminal_for(operation, request.authority)
    store.close()

    restarted_store = RemoteModuleVolumePreparationStore(evidence_root)
    appliance.created_flags = None
    replay = await DurableRemoteModuleVolumePreparationHost(restarted_store, host).execute(request)
    assert replay == response
    assert appliance.created_flags is None
    restarted_store.close()
    executor.shutdown()
