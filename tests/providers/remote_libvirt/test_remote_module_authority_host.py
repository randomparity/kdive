"""Concrete remote authority-host module preparation boundary."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from kdive.db.remote_module_attempt_obligations import (
    ModuleAttemptRestoredEvidence,
    ModuleAttemptTerminalEvidence,
    ModuleAttemptWorkerWriteContext,
)
from kdive.domain.remote_module_attempt_preparation import (
    ModuleAttemptObligationReceiptV1,
    ModuleAttemptPreparationRequestV1,
)
from kdive.providers.external_boot_authority.protocol import (
    AuthorityMutationRequestV1,
    AuthorityPreparationMutationRequestV1,
)
from kdive.providers.remote_libvirt.external_boot_authority import (
    DurableRemoteModuleVolumePreparationHost,
    RemoteModuleLifecycleRequestV1,
    RemoteModuleLifecycleResponseV1,
    RemoteModulePreparationBeginResponseV1,
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
    identity_for,
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
from kdive.providers.remote_libvirt.lifecycle.xml import overlay_volume_name
from kdive.providers.remote_libvirt.remote_module_authority_host import (
    ConcreteRemoteModuleAuthorityHost,
    FactoryRemoteModuleAuthorityHost,
    RemoteModuleAuthorityHostConfiguration,
    RemoteModuleAuthorityHostFactory,
    load_installed_remote_module_appliance,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.remote_module_authority_preparation import (
    execute_remote_module_lifecycle_on_authority_host,
)
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
from tests.providers.remote_libvirt.lifecycle.rootfs.remote_module_volumes_support import Volume
from tests.support.external_boot_plan import external_boot_plan


def _request() -> RemoteModuleVolumePreparationRequestV1:
    operation = module_operation()
    system_id = UUID(operation.system_id)
    run_id = UUID(operation.run_id)
    plan = external_boot_plan(system_id, run_id)
    operation = operation.model_copy(
        update={
            "plan_identity": plan.identity,
            "release": plan.module_obligation.release,
            "source_manifest": plan.module_obligation.source_manifest,
        }
    )
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
    return RemoteModuleVolumePreparationRequestV1(authority=authority, operation=operation)


class _Writer:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._evidence: SourceFilesystemEvidence | None = None

    def build(self, operation: bytes, entries: tuple[ModuleTreeEntry, ...]) -> BuiltSourceImage:
        self._path.write_bytes(b"source-image".ljust(4096, b"\0"))
        self._evidence = SourceFilesystemEvidence(
            operation=operation,
            manifest=json.loads(operation)["source_manifest"],
            entry_count=len(entries),
            content_bytes=sum(len(entry.content or b"") for entry in entries),
        )
        return BuiltSourceImage(self._path, 4096, self._evidence)

    def inspect(self, path: Path) -> SourceFilesystemEvidence:
        assert path.read_bytes() == b"source-image".ljust(4096, b"\0")
        assert self._evidence is not None
        return self._evidence

    def build_from_archive(self, operation: bytes, archive: Path) -> BuiltSourceImage:
        assert archive.read_bytes() == b"canonical-module-archive"
        return self.build(operation, (ModuleTreeEntry("kernel.ko", 0o100644, content=b"abc"),))


class _Stager:
    def materialize_artifacts(
        self, plan: object, directory_fd: int
    ) -> tuple[dict[str, object], str]:
        request_plan = _request().authority.plan
        assert plan == request_plan
        descriptor = os.open(
            "modules", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd
        )
        try:
            os.write(descriptor, b"canonical-module-archive")
        finally:
            os.close(descriptor)
        return (
            {
                "release": request_plan.module_obligation.release,
                "module_source_manifest": request_plan.module_obligation.source_manifest,
            },
            "sha256:" + "0" * 64,
        )


def _install_appliance(root: Path, architecture: str = "x86_64") -> tuple[Path, str]:
    directory = root / architecture
    image = directory / "image"
    image.mkdir(parents=True)
    assets = {
        "image/vmlinuz": b"kernel",
        "image/initramfs.cpio": b"initramfs",
    }
    for name, content in assets.items():
        (directory / name).write_bytes(content)
    manifest = {
        "architecture": architecture,
        "files": [
            {
                "path": name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
            for name, content in assets.items()
        ],
        "format": "kdive-remote-module-appliance-v1",
        "initramfs_files": [],
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    (directory / "manifest.json").write_bytes(manifest_bytes)
    return directory, "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()


@pytest.mark.anyio
async def test_production_factory_binds_overlay_and_verified_installed_appliance(
    tmp_path: Path,
) -> None:
    request = _request()
    appliance_root = tmp_path / "appliance"
    appliance_directory, manifest_digest = _install_appliance(appliance_root)
    connection = StorageConn()
    root_name = overlay_volume_name(request.authority.system_id)
    root_path = tmp_path / root_name
    root_path.write_bytes(b"root")
    root_volume = Volume(f"<volume><name>{root_name}</name></volume>", 4096, root_name)
    root_volume.backing_path = str(root_path)
    connection.pool.volumes[root_name] = root_volume
    executor = RemoteModulePreparationExecutor()
    clock = Clock()
    factory = RemoteModuleAuthorityHostFactory(
        connection=connection,
        pool_name="systems",
        architectures=("x86_64",),
        work_dir=tmp_path,
        appliance_root=appliance_root,
        artifact_stager=_Stager(),
        secret_registry=SecretRegistry(),
        executor=executor,
        monotonic=clock,
    )
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    store = RemoteModuleVolumePreparationStore(evidence_root)
    durable = DurableRemoteModuleVolumePreparationHost(
        store, FactoryRemoteModuleAuthorityHost(factory), monotonic=clock
    )
    preparation = ModuleAttemptPreparationRequestV1(
        module_attempt_obligation=ModuleAttemptObligationReceiptV1(
            system_id=request.authority.system_id,
            run_id=request.authority.run_id,
            operation_nonce="9" * 32,
        )
    )

    begin = await durable.begin(request.authority, preparation, 45)

    assert isinstance(begin, RemoteModulePreparationBeginResponseV1)
    assert begin.preparation == preparation
    assert begin.operation.root_volume.key == root_name
    assert begin.operation.root_volume.identity.startswith("sha256:")
    assert begin.operation.appliance_image_digest == manifest_digest
    configuration = factory.configuration("x86_64")
    assert configuration.appliance.appliance_volume is None
    assert configuration.appliance.appliance_kernel == appliance_directory / "image/vmlinuz"
    assert configuration.appliance.appliance_initrd == appliance_directory / "image/initramfs.cpio"
    admitted = store.reopen_request(
        RemoteModuleVolumePreparationRequestV1(
            authority=request.authority, operation=begin.operation
        )
    )
    assert admitted.local_deadline == 45.0
    store.close()
    executor.shutdown()


def test_production_factory_rejects_architecture_outside_fixed_selection(
    tmp_path: Path,
) -> None:
    executor = RemoteModulePreparationExecutor()
    factory = RemoteModuleAuthorityHostFactory(
        connection=StorageConn(),
        pool_name="systems",
        architectures=("x86_64",),
        work_dir=tmp_path,
        appliance_root=tmp_path / "appliance",
        artifact_stager=_Stager(),
        secret_registry=SecretRegistry(),
        executor=executor,
        monotonic=Clock(),
    )

    with pytest.raises(ValueError, match="not enabled"):
        factory.configuration("ppc64le")
    executor.shutdown()


def test_installed_appliance_loader_rejects_tampered_direct_kernel(tmp_path: Path) -> None:
    directory, _digest = _install_appliance(tmp_path)
    (directory / "image/vmlinuz").write_bytes(b"tamper")

    with pytest.raises(ValueError, match="installed appliance file"):
        load_installed_remote_module_appliance(tmp_path, "x86_64")


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


def _lifecycle_authority(
    request: RemoteModuleVolumePreparationRequestV1,
    *,
    purpose: str,
    operation: str,
) -> AuthorityMutationRequestV1:
    authority = request.authority
    return AuthorityMutationRequestV1.model_validate(
        {
            **authority.model_dump(
                mode="python",
                by_alias=True,
                exclude={"plan", "purpose", "operation", "operation_identity"},
            ),
            "purpose": purpose,
            "operation": operation,
            "operation_identity": f"{operation}-op",
        }
    )


@pytest.mark.anyio
@pytest.mark.parametrize("restore_first", [True, False])
async def test_concrete_host_creates_streams_runs_reopens_and_tears_down(
    tmp_path: Path,
    restore_first: bool,
) -> None:
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
    current_result = [durable_result]
    volume_config = RemoteModuleVolumePreparation(
        storage=storage,
        pool_name="systems",
        entries=(),
        writer=_Writer(tmp_path / "source.ext4"),
        inspect_attachments=lambda identity_port, present_attempt_volumes=None: detached,
        work_dir=tmp_path,
        artifact_stager=_Stager(),
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
        read_scratch_result=lambda _volume, _deadline: current_result[0].to_wire_bytes(),
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
    store.stage(request, 10_000.0)
    response = await durable_host.execute(request)

    assert isinstance(response, RemoteModuleTerminalPreparationResponseV1)
    assert set(storage.pool.volumes) == {source_name, scratch_name}
    assert appliance.created_flags is not None
    assert appliance.domain is not None and appliance.domain.destroyed
    assert response.result == durable_result
    assert response.recovery.source_volume.ref == source_name
    response.validate_terminal_for(operation, request.authority)

    restored_operation = RemoteModuleOperationV1(
        operation="restore",
        system_id=operation.system_id,
        run_id=operation.run_id,
        plan_identity=operation.plan_identity,
        operation_nonce=operation.operation_nonce,
        release=operation.release,
        root_volume=operation.root_volume,
        source_manifest=operation.source_manifest,
        capture_absent=True,
        installed_manifest=operation.source_manifest,
        appliance_image_digest=operation.appliance_image_digest,
    )
    restored_result = durable_result.model_copy(
        update={"phase": "restored", "entry_count": None, "content_bytes": None}
    )
    restored_result.validate_for(restored_operation)
    current_result[0] = restored_result
    restore_request = RemoteModuleLifecycleRequestV1(
        authority=_lifecycle_authority(request, purpose="recover", operation="recover"),
        action="restore",
        budget_seconds=300,
    )
    preparation = ModuleAttemptPreparationRequestV1(
        module_attempt_obligation=ModuleAttemptObligationReceiptV1(
            system_id=request.authority.system_id,
            run_id=request.authority.run_id,
            operation_nonce=operation.operation_nonce,
        )
    )
    worker_context = ModuleAttemptWorkerWriteContext(
        job_id=uuid4(),
        job_attempt=1,
        incarnation_credential=SecretStr("worker-credential"),
        preparation=preparation,
    )
    assert response.recovery.installed_entry_count is not None
    assert response.recovery.installed_content_bytes is not None
    terminal_evidence = ModuleAttemptTerminalEvidence(
        terminal_operation=operation.model_dump(mode="json"),
        terminal_operation_identity=identity_for(operation),
        terminal_result=durable_result.model_dump(mode="json"),
        terminal_result_identity=identity_for(durable_result),
        baseline_operation_identity=response.recovery.operation_identity,
        baseline_result_identity=response.recovery.result_identity,
        installed_entry_count=response.recovery.installed_entry_count,
        installed_content_bytes=response.recovery.installed_content_bytes,
        recovery_reference=response.recovery.model_dump(mode="json"),
    )

    class Repository:
        evidence: list[object] = []
        restored_evidence: ModuleAttemptRestoredEvidence | None = None
        discharged = False

        async def worker_record_restored_evidence(self, *_args: object) -> bool:
            assert _args[0] is worker_connection
            assert isinstance(_args[-1], ModuleAttemptRestoredEvidence)
            self.evidence.append(_args[-1])
            self.restored_evidence = _args[-1]
            return True

        async def read_restored_evidence(self, *_args: object) -> object:
            assert _args[0] is worker_connection
            return self.restored_evidence

        async def read_terminal_evidence(self, *_args: object) -> object:
            assert _args[0] is worker_connection
            return terminal_evidence

        async def worker_discharge_reap_obligation(self, *_args: object) -> bool:
            assert _args[0] is worker_connection
            self.discharged = True
            return True

        async def reap_obligation_is_open(self, *_args: object) -> bool:
            assert _args[0] is worker_connection
            return True

    class Sender:
        async def execute_remote_module_lifecycle(
            self, candidate: RemoteModuleLifecycleRequestV1, *, deadline: float
        ) -> RemoteModuleLifecycleResponseV1:
            assert deadline >= asyncio.get_running_loop().time()
            return await durable_host.execute_lifecycle(candidate)

    repository = Repository()
    worker_connection = object()
    restored = None
    if restore_first:
        restored = await execute_remote_module_lifecycle_on_authority_host(
            connection=cast(Any, worker_connection),
            repository=cast(Any, repository),
            sender=cast(Any, Sender()),
            authority=restore_request.authority,
            preparation=preparation,
            worker_context=worker_context,
            action="restore",
            deadline=asyncio.get_running_loop().time() + 10,
        )
        assert restored.operation == restored_operation
        assert restored.result == restored_result
        assert not restored.volumes_absent
    else:
        current_result[0] = durable_result

    reap_purpose, reap_operation = (
        ("release", "cleanup") if restore_first else ("teardown", "teardown")
    )
    reap_request = RemoteModuleLifecycleRequestV1(
        authority=_lifecycle_authority(request, purpose=reap_purpose, operation=reap_operation),
        action="reap",
        budget_seconds=300,
    )
    reaped = await execute_remote_module_lifecycle_on_authority_host(
        connection=cast(Any, worker_connection),
        repository=cast(Any, repository),
        sender=cast(Any, Sender()),
        authority=reap_request.authority,
        preparation=preparation,
        worker_context=worker_context,
        action="reap",
        deadline=asyncio.get_running_loop().time() + 10,
    )
    assert reaped.volumes_absent
    assert reaped.operation == (restored_operation if restore_first else operation)
    assert reaped.result == (restored_result if restore_first else durable_result)
    assert storage.pool.volumes[source_name].deleted
    assert storage.pool.volumes[scratch_name].deleted
    reaping_name = render_module_volume_name(
        operation.system_id, operation.run_id, operation.operation_nonce, "reaping.journal"
    )
    reaped_name = render_module_volume_name(
        operation.system_id, operation.run_id, operation.operation_nonce, "reaped.journal"
    )
    assert {reaping_name, reaped_name} <= set(storage.pool.volumes)
    assert len(repository.evidence) == (1 if restore_first else 0)
    assert repository.discharged
    store.close()

    restarted_store = RemoteModuleVolumePreparationStore(evidence_root)
    appliance.created_flags = None
    replay = await DurableRemoteModuleVolumePreparationHost(restarted_store, host).execute(request)
    assert replay == response
    assert appliance.created_flags is None
    assert (
        await DurableRemoteModuleVolumePreparationHost(restarted_store, host).execute_lifecycle(
            reap_request
        )
        == reaped
    )
    if restored is not None:
        assert identity_for(restored.operation) == identity_for(reaped.operation)
    restarted_store.close()
    executor.shutdown()
