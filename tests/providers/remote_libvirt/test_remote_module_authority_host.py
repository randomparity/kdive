"""Concrete remote authority-host module preparation boundary."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import libvirt
import pytest
from pydantic import SecretStr

from kdive.db.remote_module_attempt_obligations import (
    ModuleAttemptRestoredEvidence,
    ModuleAttemptTerminalEvidence,
    ModuleAttemptWorkerWriteContext,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.remote_module_attempt_preparation import (
    ModuleAttemptObligationReceiptV1,
    ModuleAttemptPreparationRequestV1,
)
from kdive.providers.external_boot_authority.protocol import (
    AuthorityMutationRequestV1,
    AuthorityPreparationMutationRequestV1,
    AuthorityTeardownMutationRequestV1,
)
from kdive.providers.ports.external_boot import ExternalBootActivationBinding
from kdive.providers.remote_libvirt.authority_client import (
    RemoteModuleAuthorityAdapter,
    module_completion,
)
from kdive.providers.remote_libvirt.external_boot_authority import (
    DurableRemoteModuleVolumePreparationHost,
    RemoteModuleLifecycleRequestV1,
    RemoteModuleLifecycleResponseV1,
    RemoteModulePreparationBeginResponseV1,
    RemoteModuleSystemTeardownRequestV1,
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
from kdive.providers.shared.runtime_paths import domain_name_for
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.remote_module_authority_preparation import (
    execute_remote_module_lifecycle_on_authority_host,
)
from tests.providers.remote_libvirt.lifecycle.rootfs.remote_module_appliance_support import (
    UUID1,
    Clock,
    Domain,
    Executor,
    libvirt_error,
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


class _SystemDomain(Domain):
    """The System's own libvirt domain: ``destroy`` really stops it."""

    def __init__(self, *, active: bool) -> None:
        super().__init__("<domain/>", active=active, persistent=True)
        self.started = False

    def destroy(self) -> int:
        self.destroyed = True
        self.active = False
        return 0

    def create(self) -> int:
        self.started = True
        self.active = True
        return 0


class _LostResponseDomain(_SystemDomain):
    def destroy(self) -> int:
        super().destroy()
        raise libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR)


class _StuckDomain(_SystemDomain):
    def destroy(self) -> int:
        self.destroyed = True
        raise libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR)


class _IgnoredDestroyDomain(_SystemDomain):
    def destroy(self) -> int:
        self.destroyed = True
        return 0


class _SystemConn(ApplianceConn):
    """Appliance connection that also serves the System domain and notes when it is created."""

    def __init__(self, *args: Any, system: _SystemDomain | None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.system = system
        self.created_while_system_active: list[bool] = []

    def lookupByName(self, name: str) -> Any:  # noqa: N802
        if name == domain_name_for(UUID(UUID1)):
            if self.system is None:
                raise libvirt_error(libvirt.VIR_ERR_NO_DOMAIN)
            return self.system
        return super().lookupByName(name)

    def createXML(self, xml: str, flags: int = 0) -> Any:  # noqa: N802
        self.created_while_system_active.append(bool(self.system and self.system.active))
        return super().createXML(xml, flags)


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
    identity: str | None = None,
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
            "operation_identity": identity or f"{operation}-op",
        }
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("restore_first", "reap_purpose", "reap_operation"),
    [
        (True, "release", "cleanup"),
        (False, "teardown", "teardown"),
    ],
)
async def test_concrete_host_creates_streams_runs_reopens_and_tears_down(
    tmp_path: Path,
    restore_first: bool,
    reap_purpose: str,
    reap_operation: str,
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
    storage.seed_root(operation.root_volume.key)
    clock = Clock()
    appliance = _SystemConn([], clock, system=_SystemDomain(active=False))
    durable_result = _result(operation)
    current_result = [durable_result]

    def read_scratch(volume: PreparedVolume, deadline: float) -> bytes:
        del deadline
        assert not storage.pool.volumes[volume.name].deleted
        return current_result[0].to_wire_bytes()

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
        read_scratch_result=read_scratch,
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
        async def _request(
            self,
            operation: str,
            candidate: RemoteModuleLifecycleRequestV1,
            model: object,
            *,
            deadline: float,
        ) -> RemoteModuleLifecycleResponseV1:
            assert operation == "execute-remote-module-lifecycle"
            assert model is RemoteModuleLifecycleResponseV1
            assert deadline >= asyncio.get_running_loop().time()
            return await durable_host.execute_lifecycle(candidate)

    repository = Repository()
    worker_connection = object()
    restored = None
    if restore_first:
        restored = await execute_remote_module_lifecycle_on_authority_host(
            connection=cast(Any, worker_connection),
            repository=cast(Any, repository),
            sender=cast(Any, RemoteModuleAuthorityAdapter(cast(Any, Sender()))),
            authority=restore_request.authority,
            preparation=preparation,
            worker_context=worker_context,
            action="restore",
            deadline=asyncio.get_running_loop().time() + 10,
        )
        assert restored.operation.evidence.document == restored_operation.model_dump(mode="json")
        assert restored.result.document == restored_result.model_dump(mode="json")
        assert not restored.volumes_absent
    else:
        current_result[0] = durable_result

    reap_request = RemoteModuleLifecycleRequestV1(
        authority=_lifecycle_authority(request, purpose=reap_purpose, operation=reap_operation),
        action="reap",
        budget_seconds=300,
    )
    reaped = await execute_remote_module_lifecycle_on_authority_host(
        connection=cast(Any, worker_connection),
        repository=cast(Any, repository),
        sender=cast(Any, RemoteModuleAuthorityAdapter(cast(Any, Sender()))),
        authority=reap_request.authority,
        preparation=preparation,
        worker_context=worker_context,
        action="reap",
        deadline=asyncio.get_running_loop().time() + 10,
    )
    assert reaped.volumes_absent
    assert reaped.operation.evidence.document == (
        restored_operation if restore_first else operation
    ).model_dump(mode="json")
    assert reaped.result.document == (
        restored_result if restore_first else durable_result
    ).model_dump(mode="json")
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
    replayed_lifecycle = await DurableRemoteModuleVolumePreparationHost(
        restarted_store, host
    ).execute_lifecycle(reap_request)
    assert module_completion(replayed_lifecycle.operation, replayed_lifecycle) == reaped
    if restored is not None:
        assert restored.operation.evidence.identity == reaped.operation.evidence.identity
    restarted_store.close()
    executor.shutdown()


class _Quiesce:
    """A concrete host over fakes whose attachment inspection tracks the System's power."""

    def __init__(
        self,
        tmp_path: Path,
        system: _SystemDomain | None,
        *,
        staging: bool = True,
        appliance_digest: str | None = None,
        deadline: float = 10_000.0,
        fail: bool = False,
        appliance_present: bool = False,
        fixed_inspection: bool = False,
    ) -> None:
        self.request = _request()
        operation = self.request.operation
        self.scratch = _result(operation)
        names = [
            render_module_volume_name(
                operation.system_id, operation.run_id, operation.operation_nonce, purpose
            )
            for purpose in ("source.ext4", "scratch.ext4")
        ]
        detached = AttachmentInspection(
            system_shut_off=True,
            exclusive=True,
            appliance_present=False,
            detached_volumes=frozenset(("systems", name) for name in names),
        )
        after_failure = replace(detached, appliance_present=appliance_present)
        self.storage = StorageConn()
        self.storage.seed_root(operation.root_volume.key)
        clock = self.clock = Clock()
        self.appliance = _SystemConn([], clock, system=system)

        def inspect() -> AttachmentInspection:
            if system is not None and system.active:
                raise CategorizedError("owning System is active", category=ErrorCategory.CONFLICT)
            return detached

        volume_config = RemoteModuleVolumePreparation(
            storage=self.storage,
            pool_name="systems",
            entries=(),
            writer=_Writer(tmp_path / "source.ext4"),
            inspect_attachments=lambda identity_port, present_attempt_volumes=None: (
                after_failure if fail else detached
            ),
            work_dir=tmp_path,
            artifact_stager=_Stager() if staging else None,
        )
        appliance_config = RemoteModuleApplianceExecution(
            appliance=self.appliance,
            architecture="x86_64",
            emulator_path="/usr/bin/qemu-system-x86_64",
            memory_kib=262_144,
            vcpus=1,
            appliance_volume="appliance-x86_64.qcow2",
            appliance_image_digest=appliance_digest or operation.appliance_image_digest,
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
            read_scratch_result=lambda volume, deadline: (
                None if fail else self.scratch.to_wire_bytes()
            ),
            inspect_attachments=inspect,
            secret_registry=SecretRegistry(),
            deadline_executor=Executor(),
            monotonic=clock,
        )
        self.executor = RemoteModulePreparationExecutor()
        host = self.host = ConcreteRemoteModuleAuthorityHost(
            RemoteModuleAuthorityHostConfiguration(
                volume_config, appliance_config, _Identity(), fixed_inspection=fixed_inspection
            ),
            self.executor,
        )
        evidence_root = tmp_path / "evidence"
        evidence_root.mkdir(mode=0o700)
        self.store = RemoteModuleVolumePreparationStore(evidence_root)
        self.store.stage(self.request, deadline)
        self.durable = DurableRemoteModuleVolumePreparationHost(self.store, host)

    def _binding(self) -> ExternalBootActivationBinding:
        authority = self.request.authority
        return ExternalBootActivationBinding(
            system_id=str(authority.system_id),
            run_id=str(authority.run_id),
            activation_id=str(authority.activation_id),
        )

    def prior_power(self) -> str | None:
        return self.store.prior_power(self._binding(), self.request.authority.plan_identity)

    def _restore_request(self) -> RemoteModuleLifecycleRequestV1:
        """Model the appliance rewriting the scratch volume, and return the RECOVER restore."""
        self.scratch = self.scratch.model_copy(
            update={"phase": "restored", "entry_count": None, "content_bytes": None}
        )
        return RemoteModuleLifecycleRequestV1(
            authority=_lifecycle_authority(self.request, purpose="release", operation="recover"),
            action="restore",
            budget_seconds=300,
        )

    async def restore(self) -> RemoteModuleLifecycleResponseV1:
        """Run the release RECOVER module restore after a completed PREPARE."""
        return await self.durable.execute_lifecycle(self._restore_request())

    async def crash_after_host_restore(self) -> None:
        """Finish the host restore, then die before the durable layer publishes anything."""
        request = self._restore_request()
        self.store.stage_lifecycle(request, 10_000.0)
        admitted = self.store.reopen_lifecycle(request)
        terminal = self.store.reopen_terminal(self._binding(), request.authority.plan_identity)
        await self.host.execute_lifecycle(request, terminal, None, admitted.local_deadline)

    async def reap(
        self, operation: str, identity: str | None = None
    ) -> RemoteModuleLifecycleResponseV1:
        """Run a module reap with the purpose and operation of a release CLEANUP or a teardown."""
        purpose = "release" if operation == "cleanup" else operation
        return await self.durable.execute_lifecycle(
            RemoteModuleLifecycleRequestV1(
                authority=_lifecycle_authority(
                    self.request, purpose=purpose, operation=operation, identity=identity
                ),
                action="reap",
                budget_seconds=300,
            )
        )

    async def system_teardown(self) -> RemoteModuleLifecycleResponseV1:
        """Run the System teardown's module reap, as the authority does after destroying it."""
        values = self.request.authority.model_dump(
            mode="python",
            by_alias=True,
            exclude={
                "schema_",
                "plan",
                "purpose",
                "operation",
                "operation_identity",
                "expected_source_identity",
                "intended_target_identity",
                "recovery_objects",
                "local_timing",
            },
        )
        authority = AuthorityTeardownMutationRequestV1.model_validate(
            values | {"purpose": "teardown", "operation": "teardown", "operation_identity": "td"}
        )
        return await self.durable.execute_system_teardown(
            RemoteModuleSystemTeardownRequestV1(authority=authority, budget_seconds=300)
        )

    def close(self) -> None:
        self.store.close()
        self.executor.shutdown()


@pytest.mark.anyio
async def test_running_system_is_recorded_then_stopped_before_the_appliance(
    tmp_path: Path,
) -> None:
    system = _SystemDomain(active=True)
    stack = _Quiesce(tmp_path, system)
    response = await stack.durable.execute(stack.request)

    assert isinstance(response, RemoteModuleTerminalPreparationResponseV1)
    assert system.destroyed and not system.active
    assert stack.prior_power() == "running"
    assert stack.appliance.created_while_system_active == [False]
    stack.close()


@pytest.mark.anyio
async def test_stopped_system_is_recorded_inactive_and_never_destroyed(tmp_path: Path) -> None:
    system = _SystemDomain(active=False)
    stack = _Quiesce(tmp_path, system)
    await stack.durable.execute(stack.request)

    assert not system.destroyed
    assert stack.prior_power() == "inactive"
    stack.close()


@pytest.mark.anyio
async def test_retry_after_the_stop_landed_keeps_the_recorded_running_power(
    tmp_path: Path,
) -> None:
    """The first worker died after the destroy; the retry finds an inactive domain."""
    system = _SystemDomain(active=False)
    stack = _Quiesce(tmp_path, system)
    stack.store.record_prior_power(stack.request.authority, "running")
    await stack.durable.execute(stack.request)

    assert stack.prior_power() == "running"
    stack.close()


@pytest.mark.anyio
async def test_destroy_whose_response_was_lost_is_accepted_when_the_domain_is_inactive(
    tmp_path: Path,
) -> None:
    system = _LostResponseDomain(active=True)
    stack = _Quiesce(tmp_path, system)
    response = await stack.durable.execute(stack.request)

    assert isinstance(response, RemoteModuleTerminalPreparationResponseV1)
    assert stack.appliance.created_while_system_active == [False]
    stack.close()


@pytest.mark.anyio
async def test_restore_stops_the_activated_system_before_the_appliance(tmp_path: Path) -> None:
    """A release restores modules offline, so the running activated guest is stopped first."""
    system = _SystemDomain(active=False)
    stack = _Quiesce(tmp_path, system)
    await stack.durable.execute(stack.request)
    system.active = True

    restored = await stack.restore()

    assert restored.result.phase == "restored"
    assert system.destroyed and not system.active
    stack.close()


@pytest.mark.anyio
async def test_restore_never_destroys_a_stopped_system(tmp_path: Path) -> None:
    system = _SystemDomain(active=False)
    stack = _Quiesce(tmp_path, system)
    await stack.durable.execute(stack.request)

    restored = await stack.restore()

    assert restored.result.phase == "restored"
    assert not system.destroyed
    stack.close()


@pytest.mark.anyio
async def test_restore_accepts_a_destroy_whose_response_was_lost(tmp_path: Path) -> None:
    system = _LostResponseDomain(active=False)
    stack = _Quiesce(tmp_path, system)
    await stack.durable.execute(stack.request)
    system.active = True

    restored = await stack.restore()

    assert restored.result.phase == "restored"
    assert not system.active
    stack.close()


@pytest.mark.anyio
@pytest.mark.parametrize("domain_type", [_StuckDomain, _IgnoredDestroyDomain])
async def test_restore_refuses_a_system_that_cannot_be_stopped(
    tmp_path: Path, domain_type: type[_SystemDomain]
) -> None:
    system = domain_type(active=False)
    stack = _Quiesce(tmp_path, system)
    await stack.durable.execute(stack.request)
    system.active = True

    with pytest.raises(CategorizedError, match="System domain") as failed:
        await stack.restore()

    assert failed.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert system.active
    stack.close()


@pytest.mark.anyio
async def test_system_teardown_after_a_restore_reaps_against_the_restored_result(
    tmp_path: Path,
) -> None:
    """A release that failed after RECOVER leaves the restored result in the scratch volume."""
    stack = _Quiesce(tmp_path, _SystemDomain(active=False))
    await stack.durable.execute(stack.request)
    await stack.restore()

    reaped = await stack.system_teardown()

    assert reaped.volumes_absent
    assert reaped.result.phase == "restored"
    assert await stack.system_teardown() == reaped
    stack.close()


@pytest.mark.anyio
async def test_system_teardown_without_a_restore_reaps_against_the_installed_result(
    tmp_path: Path,
) -> None:
    stack = _Quiesce(tmp_path, _SystemDomain(active=False))
    await stack.durable.execute(stack.request)

    reaped = await stack.system_teardown()

    assert reaped.volumes_absent
    assert reaped.result.phase == "installed"
    stack.close()


@pytest.mark.anyio
@pytest.mark.parametrize("restore_first", [True, False])
async def test_system_teardown_refuses_a_scratch_result_that_is_not_the_stored_evidence(
    tmp_path: Path, restore_first: bool
) -> None:
    """With a stored restore only the restored result is acceptable; without, the installed one."""
    stack = _Quiesce(tmp_path, _SystemDomain(active=False))
    installed = stack.scratch
    await stack.durable.execute(stack.request)
    if restore_first:
        await stack.restore()
        stack.scratch = installed
    else:
        stack.scratch = installed.model_copy(update={"entry_count": 99, "content_bytes": 99})

    with pytest.raises(ValueError, match="differs before reap"):
        await stack.system_teardown()

    assert stack.storage.pool.volumes
    stack.close()


@pytest.mark.anyio
async def test_system_teardown_after_a_crash_before_the_restore_evidence_reaps_the_restored_result(
    tmp_path: Path,
) -> None:
    """The scratch volume's finished restore stands in for the evidence the crash lost (#3427)."""
    stack = _Quiesce(tmp_path, _SystemDomain(active=False))
    await stack.durable.execute(stack.request)
    await stack.crash_after_host_restore()

    reaped = await stack.system_teardown()

    assert reaped.volumes_absent
    assert reaped.result == stack.scratch
    assert reaped.operation.operation == "restore"
    assert not _live_attempt_volumes(stack)
    stack.close()


@pytest.mark.anyio
async def test_restore_retry_after_a_crash_before_its_evidence_publishes_it(
    tmp_path: Path,
) -> None:
    stack = _Quiesce(tmp_path, _SystemDomain(active=False))
    await stack.durable.execute(stack.request)
    await stack.crash_after_host_restore()

    restored = await stack.restore()

    assert restored.result == stack.scratch
    assert stack.store.reopen_restored(stack._binding(), stack.request.authority.plan_identity)
    assert (await stack.reap("cleanup")).volumes_absent
    stack.close()


@pytest.mark.anyio
async def test_crash_between_the_restore_evidence_writes_is_recovered_by_a_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whichever evidence file is written first, a retry must leave both behind."""
    stack = _Quiesce(tmp_path, _SystemDomain(active=False))
    await stack.durable.execute(stack.request)
    publish = stack.store._publish
    writes = 0

    def die_on_the_second_evidence_write(name: str, data: bytes) -> None:
        nonlocal writes
        if name.endswith((".lifecycle-completion", ".restored")):
            writes += 1
            if writes == 2:
                raise SystemExit("worker died between the restore evidence writes")
        publish(name, data)

    monkeypatch.setattr(stack.store, "_publish", die_on_the_second_evidence_write)
    with pytest.raises(SystemExit):
        await stack.restore()
    monkeypatch.setattr(stack.store, "_publish", publish)

    restored = await stack.restore()

    assert restored.result == stack.scratch
    assert (await stack.reap("cleanup")).volumes_absent
    stack.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "unfinished",
    [
        {"phase": "restore-ready"},
        {"status": "failure", "error_code": "FILESYSTEM_FAILURE"},
    ],
)
async def test_system_teardown_refuses_an_unfinished_restore_in_the_scratch_volume(
    tmp_path: Path, unfinished: dict[str, str]
) -> None:
    stack = _Quiesce(tmp_path, _SystemDomain(active=False))
    await stack.durable.execute(stack.request)
    await stack.crash_after_host_restore()
    stack.scratch = stack.scratch.model_copy(update=unfinished)

    with pytest.raises(ValueError, match="differs before reap"):
        await stack.system_teardown()

    assert len(_live_attempt_volumes(stack)) == 2
    stack.close()


def _live_attempt_volumes(stack: _Quiesce) -> set[str]:
    operation = stack.request.operation
    names = {
        render_module_volume_name(
            operation.system_id, operation.run_id, operation.operation_nonce, purpose
        )
        for purpose in ("source.ext4", "scratch.ext4")
    }
    volumes = stack.storage.pool.volumes
    return {name for name in names if name in volumes and not volumes[name].deleted}


class _PowerAwareInspection:
    """Fixed attachment inspection over a fake System; an active owner needs the allow flag."""

    def __init__(self, stack: _Quiesce, system: _SystemDomain) -> None:
        operation = stack.request.operation
        self.detached = frozenset(
            (
                "systems",
                render_module_volume_name(
                    operation.system_id, operation.run_id, operation.operation_nonce, purpose
                ),
            )
            for purpose in ("source.ext4", "scratch.ext4")
        )
        self.system = system
        self.allowed: list[bool] = []

    def __call__(
        self,
        conn: object,
        identity: object,
        expected: object,
        present: object = None,
        *,
        allow_cleanup_partial: bool = False,
        allow_active_system: bool = False,
    ) -> AttachmentInspection:
        del conn, identity, expected, present, allow_cleanup_partial
        self.allowed.append(allow_active_system)
        active = bool(self.system.active)
        if active and not allow_active_system:
            raise CategorizedError("owning System is active", category=ErrorCategory.CONFLICT)
        return AttachmentInspection(not active, True, False, self.detached)


async def _restored_stack_with_a_running_system(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[_Quiesce, _SystemDomain, _PowerAwareInspection]:
    from kdive.providers.remote_libvirt import remote_module_authority_host as host_module

    system = _SystemDomain(active=False)
    stack = _Quiesce(tmp_path, system, fixed_inspection=True)
    _seed_root(stack, _root_volume_xml("/pool/base.qcow2"))
    inspection = _PowerAwareInspection(stack, system)
    monkeypatch.setattr(host_module, "inspect_module_attachments", inspection)
    await stack.durable.execute(stack.request)
    await stack.restore()
    system.active = True
    return stack, system, inspection


@pytest.mark.anyio
async def test_release_cleanup_reap_proceeds_while_the_recovered_system_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RECOVER restores the System's power, so the reap that follows meets a running System."""
    stack, system, _inspection = await _restored_stack_with_a_running_system(tmp_path, monkeypatch)

    reaped = await stack.reap("cleanup")

    assert reaped.volumes_absent
    assert system.active and not system.destroyed
    stack.close()


def _sweep_journal_markers(stack: _Quiesce) -> None:
    """Delete the reap markers as the module-volume sweep does once the obligation discharged."""
    operation = stack.request.operation
    for kind in ("reaping.journal", "reaped.journal"):
        name = render_module_volume_name(
            operation.system_id, operation.run_id, operation.operation_nonce, kind
        )
        stack.storage.pool.volumes.pop(name)


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["cleanup", "teardown"])
async def test_reap_after_the_sweep_removed_the_markers_reports_the_volumes_absent(
    tmp_path: Path, operation: str
) -> None:
    """#3414: with no markers and no attempt volumes the reap's post-condition already holds."""
    stack = _Quiesce(tmp_path, _SystemDomain(active=False))
    installed = stack.scratch
    await stack.durable.execute(stack.request)
    await stack.restore()

    async def reap(identity: str) -> RemoteModuleLifecycleResponseV1:
        if operation == "teardown":
            return await stack.system_teardown()
        return await stack.reap(operation, identity)

    first = await reap("first")
    _sweep_journal_markers(stack)
    # The scratch volume is gone with the sweep, so nothing can still prove the restored result.
    stack.scratch = installed
    markers = set(stack.storage.pool.volumes)

    again = await reap("re-entered")

    assert again.volumes_absent
    assert again.result == first.result
    assert set(stack.storage.pool.volumes) == markers
    stack.close()


@pytest.mark.anyio
async def test_reap_with_a_volume_present_and_no_markers_still_verifies_the_result(
    tmp_path: Path,
) -> None:
    stack = _Quiesce(tmp_path, _SystemDomain(active=False))
    installed = stack.scratch
    await stack.durable.execute(stack.request)
    await stack.restore()
    stack.scratch = installed

    with pytest.raises(ValueError, match="differs before reap"):
        await stack.reap("cleanup")

    stack.close()


@pytest.mark.anyio
async def test_resolve_conflict_authority_no_longer_admits_a_reap(tmp_path: Path) -> None:
    stack = _Quiesce(tmp_path, _SystemDomain(active=False))

    with pytest.raises(ValueError, match="differs from authority operation"):
        await stack.reap("resolve-conflict")

    stack.close()


@pytest.mark.anyio
async def test_teardown_reap_still_refuses_a_running_system(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack, system, _inspection = await _restored_stack_with_a_running_system(tmp_path, monkeypatch)

    with pytest.raises(CategorizedError, match="owning System is active"):
        await stack.reap("teardown")

    assert system.active
    stack.close()


def _assert_nothing_started(stack: _Quiesce) -> None:
    assert stack.appliance.created_flags is None
    assert stack.storage.pool.volumes == {}
    assert stack.store.reopen_completion(stack.request) is None


@pytest.mark.anyio
@pytest.mark.parametrize("domain_type", [_StuckDomain, _IgnoredDestroyDomain])
async def test_system_that_survives_destroy_is_not_reported_as_refused_before_mutation(
    tmp_path: Path, domain_type: type[_SystemDomain]
) -> None:
    """Once destroy was issued the System's power may have changed, so it is no refusal."""
    system = domain_type(active=True)
    stack = _Quiesce(tmp_path, system)
    with pytest.raises(CategorizedError, match="System domain") as failed:
        await stack.durable.execute(stack.request)

    assert failed.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert failed.value.details == {"completion": "failed-after-mutation", "abandoned": True}
    assert stack.prior_power() is None
    _assert_nothing_started(stack)
    stack.close()


@pytest.mark.anyio
async def test_missing_system_domain_is_refused_before_any_mutation(tmp_path: Path) -> None:
    stack = _Quiesce(tmp_path, None)
    with pytest.raises(CategorizedError, match="could not be read") as refused:
        await stack.durable.execute(stack.request)

    assert refused.value.details == {
        "completion": "refused-before-mutation",
        "abandoned": True,
    }
    assert stack.prior_power() is None
    _assert_nothing_started(stack)
    stack.close()


@pytest.mark.anyio
async def test_preflight_refusal_leaves_a_running_system_running(tmp_path: Path) -> None:
    system = _SystemDomain(active=True)
    stack = _Quiesce(tmp_path, system, staging=False)
    with pytest.raises(CategorizedError, match="staging is not configured"):
        await stack.durable.execute(stack.request)

    assert system.active and not system.destroyed
    assert stack.prior_power() is None
    _assert_nothing_started(stack)
    stack.close()


@pytest.mark.anyio
async def test_changed_appliance_identity_is_refused_before_the_stop(tmp_path: Path) -> None:
    system = _SystemDomain(active=True)
    stack = _Quiesce(tmp_path, system, appliance_digest="sha256:" + "f" * 64)
    with pytest.raises(CategorizedError, match="identity changed") as refused:
        await stack.durable.execute(stack.request)

    assert refused.value.details == {
        "completion": "refused-before-mutation",
        "abandoned": True,
    }
    assert system.active and not system.destroyed
    _assert_nothing_started(stack)
    stack.close()


@pytest.mark.anyio
async def test_expired_deadline_leaves_a_running_system_running(tmp_path: Path) -> None:
    system = _SystemDomain(active=True)
    stack = _Quiesce(tmp_path, system, deadline=0.5)
    stack.clock.value = 1.0
    with pytest.raises(CategorizedError) as expired:
        await stack.durable.execute(stack.request)

    assert isinstance(expired.value.__cause__, TimeoutError)
    assert system.active and not system.destroyed
    _assert_nothing_started(stack)
    stack.close()


def test_prior_power_record_is_first_write_wins_and_validated(tmp_path: Path) -> None:
    store = RemoteModuleVolumePreparationStore(tmp_path)
    authority = _request().authority
    assert store.record_prior_power(authority, "running") == "running"
    assert store.record_prior_power(authority, "inactive") == "running"
    store.close()


@pytest.mark.anyio
@pytest.mark.parametrize(("active", "started"), [(True, True), (False, False)])
async def test_failed_appliance_restores_the_prior_power_without_latching(
    tmp_path: Path, active: bool, started: bool
) -> None:
    system = _SystemDomain(active=active)
    stack = _Quiesce(tmp_path, system, fail=True)
    with pytest.raises(CategorizedError, match="durable result") as failed:
        await stack.durable.execute(stack.request)

    assert failed.value.details["abandoned"] is True
    assert failed.value.details["completion"] == "failed-after-mutation"
    assert system.started is started and system.active is active
    assert stack.store.reopen_completion(stack.request) is None
    stack.close()


@pytest.mark.anyio
async def test_abandoned_preparation_may_be_executed_again(tmp_path: Path) -> None:
    system = _SystemDomain(active=True)
    stack = _Quiesce(tmp_path, system, fail=True)
    for _ in range(2):
        with pytest.raises(CategorizedError, match="durable result"):
            await stack.durable.execute(stack.request)

    assert stack.prior_power() is None
    assert system.active
    stack.close()


@pytest.mark.anyio
async def test_unrestorable_power_keeps_the_failure_latched(tmp_path: Path) -> None:
    """The appliance may still hold the root, so the System is left stopped and the latch stays."""
    system = _SystemDomain(active=True)
    stack = _Quiesce(tmp_path, system, fail=True, appliance_present=True)
    with pytest.raises(CategorizedError, match="durable result") as failed:
        await stack.durable.execute(stack.request)

    assert "abandoned" not in failed.value.details
    assert not system.started and not system.active
    completion = stack.store.reopen_completion(stack.request)
    assert completion is not None and completion.state == "failed-after-mutation"
    with pytest.raises(CategorizedError, match="requires recovery"):
        await stack.durable.execute(stack.request)


@pytest.mark.anyio
async def test_missing_result_logs_the_bounded_console_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from kdive.providers.remote_libvirt import remote_module_authority_host as host_module
    from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_appliance import (
        ApplianceOutcome,
    )

    tail = "phase=accepted\nerror_code=FILESYSTEM_FAILURE\n"
    monkeypatch.setattr(
        host_module, "run_or_adopt_appliance", lambda *_args: ApplianceOutcome(None, tail, False)
    )
    stack = _Quiesce(tmp_path, _SystemDomain(active=False))

    with (
        caplog.at_level("WARNING", logger=host_module.__name__),
        pytest.raises(CategorizedError, match="durable result"),
    ):
        await stack.durable.execute(stack.request)

    messages = [record.getMessage() for record in caplog.records]
    assert any("error_code=FILESYSTEM_FAILURE" in message for message in messages)
    assert any(f"system={stack.request.operation.system_id}" in message for message in messages)
    stack.close()


def _root_volume_xml(backing: str | None, backing_format: str | None = "qcow2") -> str:
    document = "<volume><name>root</name><capacity unit='bytes'>4096</capacity>"
    if backing is not None:
        document += f"<backingStore><path>{backing}</path>"
        if backing_format is not None:
            document += f"<format type='{backing_format}'/>"
        document += "</backingStore>"
    return document + "</volume>"


def _seed_root(stack: _Quiesce, xml: str) -> str:
    name = stack.request.operation.root_volume.key
    stack.storage.pool.foreign[name] = Volume(xml, 4096, name)
    return name


@pytest.mark.parametrize("fixed_inspection_path", [False, True])
def test_expected_attachments_carry_the_staged_base_for_every_inspection(
    tmp_path: Path, fixed_inspection_path: bool
) -> None:
    stack = _Quiesce(tmp_path, _SystemDomain(active=False))
    operation = stack.request.operation
    _seed_root(stack, _root_volume_xml("/var/lib/kdive/pool/base.qcow2"))

    if fixed_inspection_path:
        wanted = stack.host._expected_attachments(operation).appliance.root_backing_path
    else:
        wanted = stack.host._root_backing_path("systems", operation.root_volume.key)

    assert wanted == "/var/lib/kdive/pool/base.qcow2"
    stack.close()


@pytest.mark.parametrize(
    ("xml", "message"),
    [
        (_root_volume_xml("base.qcow2"), "normalized absolute"),
        (_root_volume_xml("/pool/../etc/base.qcow2"), "normalized absolute"),
        (_root_volume_xml("/pool//base.qcow2"), "normalized absolute"),
        (_root_volume_xml(" /pool/base.qcow2"), "normalized absolute"),
        (_root_volume_xml("/pool/base.qcow2", "raw"), "not qcow2"),
        (_root_volume_xml("/pool/base.qcow2", None), "not qcow2"),
    ],
)
def test_a_root_backing_path_that_is_not_the_exact_qcow2_base_is_refused(
    tmp_path: Path, xml: str, message: str
) -> None:
    stack = _Quiesce(tmp_path, _SystemDomain(active=False))
    name = _seed_root(stack, xml)

    with pytest.raises(CategorizedError, match=message) as refused:
        stack.host._root_backing_path("systems", name)

    assert refused.value.category is ErrorCategory.CONFLICT
    stack.close()


def test_a_root_volume_without_a_backing_store_renders_no_backing_path(tmp_path: Path) -> None:
    stack = _Quiesce(tmp_path, _SystemDomain(active=False))
    name = _seed_root(stack, _root_volume_xml(None))

    assert stack.host._root_backing_path("systems", name) is None
    stack.close()


def test_an_absent_root_volume_is_an_error_for_a_run_but_not_for_a_cleanup_inspection(
    tmp_path: Path,
) -> None:
    stack = _Quiesce(tmp_path, _SystemDomain(active=False))
    stack.storage.pool.foreign.clear()
    name = stack.request.operation.root_volume.key

    with pytest.raises(CategorizedError, match="backing store") as refused:
        stack.host._root_backing_path("systems", name)
    assert refused.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert stack.host._root_backing_path("systems", name, absent_ok=True) is None
    stack.close()
