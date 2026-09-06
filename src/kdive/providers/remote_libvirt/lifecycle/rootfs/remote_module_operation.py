"""Async durable-evidence runtime seam for remote module recovery (ADR-0588)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

import libvirt
from psycopg_pool import AsyncConnectionPool

from kdive.db.remote_module_attempt_obligations import (
    ModuleAttempt,
    ModuleAttemptTerminalEvidence,
    RemoteModuleAttemptObligationRepository,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.remote_module_attempt_preparation import ModuleAttemptPreparationRequestV1
from kdive.providers.infra.reaping import ModuleVolumeKey, ModuleVolumeReaper
from kdive.providers.ports.authority import AuthorityRequestSender
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_appliance import (
    ApplianceConn,
    ApplianceRequest,
    DeadlineExecutor,
    TeardownObservation,
    run_or_adopt_appliance,
    teardown_remote_module_appliance,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
    AttachmentInspection,
    RemoteDeviceIdentityPort,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleOperationV1,
    RemoteModuleRecoveryRefV2,
    RemoteModuleResultV1,
    identity_for,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_preparation import (
    RemoteModulePreparationExecutor,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volume_names import (
    render_module_volume_name,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volumes import (
    FilesystemImageWriter,
    ModuleTreeEntry,
    PreparedModuleVolumes,
    PreparedVolume,
    StorageConn,
    VolumeRequest,
    delete_owned_attempt_volume,
    prepare_attempt_volumes,
    recovery_attempt_volumes,
    validate_attempt_volumes,
)
from kdive.providers.remote_libvirt.reaping.module_volumes import (
    ModuleVolumeReaperConn,
    list_owned_module_volumes,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.remote_module_volume_preparation import (
    prepare_verified_remote_module_attempt,
)


class ModuleOperationRuntime(Protocol):
    """The phase-facing asynchronous recovery surface; it never reads libvirt metadata."""

    async def reopen_operation(
        self, recovery: RemoteModuleRecoveryRefV2
    ) -> RemoteModuleOperationV1: ...
    async def reopen_result(self, recovery: RemoteModuleRecoveryRefV2) -> RemoteModuleResultV1: ...
    async def reopen_capture_operation(
        self, recovery: RemoteModuleRecoveryRefV2
    ) -> RemoteModuleOperationV1: ...
    async def reopen_installed_result(
        self, recovery: RemoteModuleRecoveryRefV2
    ) -> RemoteModuleResultV1: ...
    async def prepare(
        self,
        request: ModuleAttemptPreparationRequestV1,
        operation: RemoteModuleOperationV1,
        executor: RemoteModulePreparationExecutor,
        authority: AuthorityRequestSender | None,
        deadline: float,
    ) -> PreparedModuleVolumes: ...
    async def run(
        self,
        operation: RemoteModuleOperationV1,
        volumes: PreparedModuleVolumes,
        executor: RemoteModulePreparationExecutor,
        deadline: float,
    ) -> RemoteModuleResultV1: ...
    async def teardown(
        self,
        recovery: RemoteModuleRecoveryRefV2,
        executor: RemoteModulePreparationExecutor,
        deadline: float,
    ) -> TeardownObservation: ...
    async def delete_source(
        self, recovery: RemoteModuleRecoveryRefV2, executor: RemoteModulePreparationExecutor
    ) -> None: ...
    async def delete_scratch(
        self, recovery: RemoteModuleRecoveryRefV2, executor: RemoteModulePreparationExecutor
    ) -> None: ...
    async def record_reaping(
        self, recovery: RemoteModuleRecoveryRefV2, executor: RemoteModulePreparationExecutor
    ) -> None: ...
    async def record_reaped(
        self, recovery: RemoteModuleRecoveryRefV2, executor: RemoteModulePreparationExecutor
    ) -> None: ...
    async def resume_reap(
        self,
        recovery: RemoteModuleRecoveryRefV2,
        executor: RemoteModulePreparationExecutor,
        deadline: float,
    ) -> TeardownObservation: ...
    async def inventory(
        self, executor: RemoteModulePreparationExecutor
    ) -> tuple[ModuleVolumeKey, ...]: ...
    async def reap(self, retained: Callable[[], Awaitable[Collection[ModuleVolumeKey]]]) -> int: ...


@dataclass(frozen=True, slots=True)
class RemoteModuleVolumePreparation:
    storage: StorageConn
    pool_name: str
    entries: tuple[ModuleTreeEntry, ...]
    writer: FilesystemImageWriter
    inspect_attachments: Callable[[RemoteDeviceIdentityPort], AttachmentInspection]
    work_dir: Path


@dataclass(frozen=True, slots=True)
class RemoteModuleVolumeRecovery:
    storage: StorageConn
    pool_name: str


@dataclass(frozen=True, slots=True)
class RemoteModuleApplianceExecution:
    appliance: ApplianceConn
    architecture: str
    emulator_path: str
    memory_kib: int
    vcpus: int
    appliance_volume: str
    appliance_image_digest: str
    root: Callable[[RemoteModuleOperationV1], PreparedVolume]
    read_scratch_result: Callable[[PreparedVolume], bytes | None]
    inspect_attachments: Callable[[], AttachmentInspection]
    secret_registry: SecretRegistry
    deadline_executor: DeadlineExecutor
    monotonic: Callable[[], float]


@dataclass(frozen=True, slots=True)
class RemoteModuleOperationRuntime:
    """Reopen scratch state first, falling back to durable evidence after scratch deletion."""

    pool: AsyncConnectionPool
    repository: RemoteModuleAttemptObligationRepository
    read_scratch_result: Callable[[RemoteModuleRecoveryRefV2], Awaitable[bytes | None]]
    volume_preparation: RemoteModuleVolumePreparation | None = None
    appliance_execution: RemoteModuleApplianceExecution | None = None
    module_volume_reaper: ModuleVolumeReaper | None = None
    volume_recovery: RemoteModuleVolumeRecovery | None = None

    @staticmethod
    def _attempt(recovery: RemoteModuleRecoveryRefV2) -> ModuleAttempt:
        return ModuleAttempt(
            UUID(recovery.system_id), UUID(recovery.run_id), recovery.operation_nonce
        )

    def _volume_request(
        self,
        operation: RemoteModuleOperationV1,
        *,
        identity: RemoteDeviceIdentityPort | None = None,
    ) -> VolumeRequest:
        configured = self.volume_preparation
        if configured is None:
            raise CategorizedError(
                "remote module volume preparation is not configured",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        return VolumeRequest(
            pool=configured.pool_name,
            system_id=operation.system_id,
            run_id=operation.run_id,
            operation_nonce=operation.operation_nonce,
            operation=operation,
            source_manifest=operation.source_manifest,
            entries=configured.entries,
            writer=configured.writer,
            inspect_attachments=(
                (lambda: configured.inspect_attachments(identity))
                if identity is not None
                else self._require_cleanup_inspection
            ),
            work_dir=configured.work_dir,
        )

    async def prepare(
        self,
        request: ModuleAttemptPreparationRequestV1,
        operation: RemoteModuleOperationV1,
        executor: RemoteModulePreparationExecutor,
        authority: AuthorityRequestSender | None,
        deadline: float,
    ) -> PreparedModuleVolumes:
        """Consume the caller's committed receipt while the verifier owns its System lock."""
        configured = self.volume_preparation
        if configured is None:
            raise CategorizedError(
                "remote module volume preparation is not configured",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        attempt = ModuleAttempt(
            UUID(operation.system_id), UUID(operation.run_id), operation.operation_nonce
        )
        expected_attempt = attempt

        def prepare_volumes(
            attempt: ModuleAttempt,
            identity: RemoteDeviceIdentityPort,
            check_deadline: Callable[[], None],
        ) -> PreparedModuleVolumes:
            if attempt != expected_attempt:
                raise CategorizedError(
                    "remote module verified attempt differs from operation",
                    category=ErrorCategory.CONFLICT,
                )
            volume_request = self._volume_request(operation, identity=identity)
            return prepare_attempt_volumes(
                configured.storage, volume_request, admit_mutation=check_deadline
            )

        return await prepare_verified_remote_module_attempt(
            self.pool,
            self.repository,
            request,
            attempt,
            executor,
            authority,
            deadline,
            prepare_volumes,
        )

    async def _evidence(
        self, recovery: RemoteModuleRecoveryRefV2
    ) -> tuple[RemoteModuleOperationV1, RemoteModuleResultV1]:
        async with self.pool.connection() as conn:
            evidence = await self.repository.read_terminal_evidence(conn, self._attempt(recovery))
        if evidence is None:
            raise CategorizedError(
                "remote module terminal evidence is absent", category=ErrorCategory.CONFLICT
            )
        try:
            operation = RemoteModuleOperationV1.model_validate(evidence.terminal_operation)
            result = RemoteModuleResultV1.model_validate(evidence.terminal_result)
            stored = RemoteModuleRecoveryRefV2.model_validate(evidence.recovery_reference)
        except ValueError:
            raise CategorizedError(
                "remote module terminal evidence is invalid", category=ErrorCategory.CONFLICT
            ) from None
        if stored != recovery or not self._matches_recovery(operation, result, recovery):
            raise CategorizedError(
                "remote module terminal evidence differs from recovery reference",
                category=ErrorCategory.CONFLICT,
            )
        if (
            identity_for(operation) != evidence.terminal_operation_identity
            or identity_for(result) != evidence.terminal_result_identity
            or evidence.baseline_operation_identity != recovery.operation_identity
            or evidence.baseline_result_identity != recovery.result_identity
        ):
            raise CategorizedError(
                "remote module terminal evidence identity is invalid",
                category=ErrorCategory.CONFLICT,
            )
        return operation, result

    async def run(
        self,
        operation: RemoteModuleOperationV1,
        volumes: PreparedModuleVolumes,
        executor: RemoteModulePreparationExecutor,
        deadline: float,
    ) -> RemoteModuleResultV1:
        configured = self.appliance_execution
        prepared = self.volume_preparation
        if configured is None or prepared is None:
            raise CategorizedError(
                "remote module appliance execution is not configured",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )

        def execute() -> RemoteModuleResultV1:
            validated = validate_attempt_volumes(prepared.storage, self._volume_request(operation))
            if validated != volumes:
                raise CategorizedError(
                    "remote module volumes differ from operation",
                    category=ErrorCategory.CONFLICT,
                )
            request = self._appliance_request(operation, validated, deadline)
            outcome = run_or_adopt_appliance(configured.appliance, request)
            if outcome.result is None:
                raise CategorizedError(
                    "remote module appliance did not produce a durable result",
                    category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                    details={"timed_out": outcome.timed_out},
                )
            raw = configured.read_scratch_result(volumes.scratch)
            if raw is None:
                raise CategorizedError(
                    "remote module result artifact is absent",
                    category=ErrorCategory.CONFLICT,
                )
            try:
                durable = RemoteModuleResultV1.from_wire_bytes(raw)
            except ValueError:
                raise CategorizedError(
                    "remote module result artifact is invalid",
                    category=ErrorCategory.CONFLICT,
                ) from None
            if durable != outcome.result:
                raise CategorizedError(
                    "remote module result changed during durable reopen",
                    category=ErrorCategory.CONFLICT,
                )
            return durable

        return await executor.run(execute)

    def _require_cleanup_inspection(self) -> AttachmentInspection:
        configured = self.appliance_execution
        if configured is None:
            raise CategorizedError(
                "remote module cleanup inspection is not configured",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        return configured.inspect_attachments()

    def _recovery_volumes(
        self, operation: RemoteModuleOperationV1, recovery: RemoteModuleRecoveryRefV2
    ) -> PreparedModuleVolumes:
        configured = self.volume_recovery
        if configured is None and self.volume_preparation is not None:
            configured = RemoteModuleVolumeRecovery(
                self.volume_preparation.storage, self.volume_preparation.pool_name
            )
        if configured is None:
            raise CategorizedError(
                "remote module volume recovery is not configured",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        volumes = recovery_attempt_volumes(
            operation, configured.pool_name, recovery.source_capacity_bytes
        )
        if (
            recovery.pool.ref != configured.pool_name
            or recovery.source_volume.ref != volumes.source.name
            or recovery.scratch_volume.ref != volumes.scratch.name
        ):
            raise CategorizedError(
                "remote module recovery volume geometry differs from fixed provider binding",
                category=ErrorCategory.CONFLICT,
            )
        return volumes

    def _volume_binding(self) -> RemoteModuleVolumeRecovery:
        if self.volume_recovery is not None:
            return self.volume_recovery
        if self.volume_preparation is not None:
            return RemoteModuleVolumeRecovery(
                self.volume_preparation.storage, self.volume_preparation.pool_name
            )
        raise CategorizedError(
            "remote module volume recovery is not configured",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )

    def _appliance_request(
        self,
        operation: RemoteModuleOperationV1,
        volumes: PreparedModuleVolumes,
        deadline: float,
    ) -> ApplianceRequest:
        configured = self.appliance_execution
        if configured is None:
            raise CategorizedError(
                "remote module appliance execution is not configured",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        return ApplianceRequest(
            name=f"kdive-module-{operation.system_id}-{operation.run_id}-{operation.operation_nonce}",
            architecture=configured.architecture,
            emulator_path=configured.emulator_path,
            memory_kib=configured.memory_kib,
            vcpus=configured.vcpus,
            pool=self._volume_binding().pool_name,
            appliance_volume=configured.appliance_volume,
            appliance_image_digest=configured.appliance_image_digest,
            root=configured.root(operation),
            source=volumes.source,
            scratch=volumes.scratch,
            operation=operation,
            secret_registry=configured.secret_registry,
            read_scratch_result=lambda: configured.read_scratch_result(volumes.scratch),
            inspect_attachments=configured.inspect_attachments,
            executor=configured.deadline_executor,
            monotonic=configured.monotonic,
            invocation_deadline=deadline,
        )

    async def teardown(
        self,
        recovery: RemoteModuleRecoveryRefV2,
        executor: RemoteModulePreparationExecutor,
        deadline: float,
    ) -> TeardownObservation:
        operation = await self.reopen_operation(recovery)
        volumes = self._recovery_volumes(operation, recovery)
        request = self._appliance_request(operation, volumes, deadline)
        configured = self.appliance_execution
        assert configured is not None
        return await executor.run(
            lambda: teardown_remote_module_appliance(configured.appliance, request)
        )

    async def _open_reap_evidence(self, recovery: RemoteModuleRecoveryRefV2) -> None:
        operation = await self.reopen_operation(recovery)
        result = await self.reopen_result(recovery)
        if recovery.installed_entry_count is None or recovery.installed_content_bytes is None:
            raise CategorizedError(
                "remote module installed baseline counts are absent",
                category=ErrorCategory.CONFLICT,
            )
        evidence = ModuleAttemptTerminalEvidence(
            terminal_operation=operation.model_dump(mode="json"),
            terminal_operation_identity=identity_for(operation),
            terminal_result=result.model_dump(mode="json"),
            terminal_result_identity=identity_for(result),
            baseline_operation_identity=recovery.operation_identity,
            baseline_result_identity=recovery.result_identity,
            installed_entry_count=recovery.installed_entry_count,
            installed_content_bytes=recovery.installed_content_bytes,
            recovery_reference=recovery.model_dump(mode="json"),
        )
        async with self.pool.connection() as conn, conn.transaction():
            await self.repository.record_terminal_evidence(conn, self._attempt(recovery), evidence)
            await self.repository.open_reap_obligation(conn, self._attempt(recovery))

    async def _delete(
        self,
        recovery: RemoteModuleRecoveryRefV2,
        executor: RemoteModulePreparationExecutor,
        purpose: str,
    ) -> None:
        if purpose == "scratch":
            await self._open_reap_evidence(recovery)
        operation = await self.reopen_operation(recovery)
        volumes = self._recovery_volumes(operation, recovery)
        selected = volumes.source if purpose == "source" else volumes.scratch
        configured = self._volume_binding()

        def delete() -> None:
            inspection = self._require_cleanup_inspection()
            delete_owned_attempt_volume(configured.storage, selected, inspection=inspection)

        await executor.run(delete)

    async def delete_source(
        self, recovery: RemoteModuleRecoveryRefV2, executor: RemoteModulePreparationExecutor
    ) -> None:
        await self._delete(recovery, executor, "source")

    async def delete_scratch(
        self, recovery: RemoteModuleRecoveryRefV2, executor: RemoteModulePreparationExecutor
    ) -> None:
        await self._delete(recovery, executor, "scratch")

    def _marker_name(self, recovery: RemoteModuleRecoveryRefV2, state: str) -> str:
        return render_module_volume_name(
            recovery.system_id, recovery.run_id, recovery.operation_nonce, f"{state}.journal"
        )

    async def _record_marker(
        self,
        recovery: RemoteModuleRecoveryRefV2,
        executor: RemoteModulePreparationExecutor,
        state: str,
    ) -> None:
        # Durable database evidence is the marker's content; the closed whole name is its
        # storage ownership proof.  Storage-volume metadata is intentionally not used.
        await self._evidence(recovery)
        configured = self._volume_binding()
        name = self._marker_name(recovery, state)

        def create() -> None:
            pool = configured.storage.storagePoolLookupByName(configured.pool_name)
            try:
                pool.storageVolLookupByName(name)
                return
            except libvirt.libvirtError as exc:
                if exc.get_error_code() != libvirt.VIR_ERR_NO_STORAGE_VOL:
                    raise
            root = ET.Element("volume")
            ET.SubElement(root, "name").text = name
            ET.SubElement(root, "capacity", unit="bytes").text = "1"
            target = ET.SubElement(root, "target")
            ET.SubElement(target, "format", type="raw")
            pool.createXML(ET.tostring(root, encoding="unicode"), 0)

        await executor.run(create)

    async def record_reaping(
        self, recovery: RemoteModuleRecoveryRefV2, executor: RemoteModulePreparationExecutor
    ) -> None:
        await self._open_reap_evidence(recovery)
        await self._record_marker(recovery, executor, "reaping")

    async def record_reaped(
        self, recovery: RemoteModuleRecoveryRefV2, executor: RemoteModulePreparationExecutor
    ) -> None:
        await self._record_marker(recovery, executor, "reaped")
        async with self.pool.connection() as conn, conn.transaction():
            await self.repository.discharge_reap_obligation(conn, self._attempt(recovery))

    async def resume_reap(
        self,
        recovery: RemoteModuleRecoveryRefV2,
        executor: RemoteModulePreparationExecutor,
        deadline: float,
    ) -> TeardownObservation:
        operation, _result = await self._evidence(recovery)
        volumes = self._recovery_volumes(operation, recovery)
        configured = self.appliance_execution
        assert configured is not None
        request = self._appliance_request(operation, volumes, deadline)
        return await executor.run(
            lambda: teardown_remote_module_appliance(configured.appliance, request)
        )

    async def inventory(
        self, executor: RemoteModulePreparationExecutor
    ) -> tuple[ModuleVolumeKey, ...]:
        configured = self._volume_binding()

        def observe() -> tuple[ModuleVolumeKey, ...]:
            return tuple(
                ModuleVolumeKey(owner.system_id, owner.run_id, owner.operation_nonce, owner.kind)
                for _name, owner in list_owned_module_volumes(
                    cast(ModuleVolumeReaperConn, configured.storage), configured.pool_name
                )
            )

        return await executor.run(observe)

    async def reap(self, retained: Callable[[], Awaitable[Collection[ModuleVolumeKey]]]) -> int:
        if self.module_volume_reaper is None:
            raise CategorizedError(
                "remote module volume reaper is not configured",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        return await self.module_volume_reaper.reap_module_volumes(retained)

    @staticmethod
    def _operation_from_result(result: RemoteModuleResultV1) -> RemoteModuleOperationV1:
        required = (
            result.system_id,
            result.run_id,
            result.plan_identity,
            result.operation_nonce,
            result.release,
            result.root_volume_key,
            result.root_volume_identity,
            result.source_manifest,
            result.appliance_image_digest,
        )
        if any(value is None for value in required):
            raise CategorizedError(
                "remote module result lacks immutable operation identity",
                category=ErrorCategory.CONFLICT,
            )
        return RemoteModuleOperationV1.model_validate(
            {
                "operation": "capture_install",
                "system_id": result.system_id,
                "run_id": result.run_id,
                "plan_identity": result.plan_identity,
                "operation_nonce": result.operation_nonce,
                "release": result.release,
                "root_volume": {
                    "key": result.root_volume_key,
                    "identity": result.root_volume_identity,
                },
                "source_manifest": result.source_manifest,
                "appliance_image_digest": result.appliance_image_digest,
            }
        )

    @staticmethod
    def _restore_operation(
        capture: RemoteModuleOperationV1, result: RemoteModuleResultV1
    ) -> RemoteModuleOperationV1:
        if (
            result.phase not in {"restore-ready", "restored"}
            or result.installed_manifest is None
            or result.capture_state is None
        ):
            raise CategorizedError(
                "remote module restore evidence is incomplete", category=ErrorCategory.CONFLICT
            )
        return RemoteModuleOperationV1(
            operation="restore",
            system_id=capture.system_id,
            run_id=capture.run_id,
            plan_identity=capture.plan_identity,
            operation_nonce=capture.operation_nonce,
            release=capture.release,
            root_volume=capture.root_volume,
            source_manifest=capture.source_manifest,
            capture_manifest=result.capture_manifest,
            capture_absent=result.capture_absent,
            installed_manifest=result.installed_manifest,
            appliance_image_digest=capture.appliance_image_digest,
        )

    @staticmethod
    def _installed_result(
        result: RemoteModuleResultV1, recovery: RemoteModuleRecoveryRefV2
    ) -> RemoteModuleResultV1:
        if result.phase == "installed":
            return result
        if (
            result.phase not in {"restore-ready", "restored"}
            or recovery.installed_entry_count is None
            or recovery.installed_content_bytes is None
        ):
            raise CategorizedError(
                "remote module installed result artifact is absent", category=ErrorCategory.CONFLICT
            )
        return result.model_copy(
            update={
                "phase": "installed",
                "entry_count": recovery.installed_entry_count,
                "content_bytes": recovery.installed_content_bytes,
            }
        )

    @classmethod
    def _matches_recovery(
        cls,
        operation: RemoteModuleOperationV1,
        result: RemoteModuleResultV1,
        recovery: RemoteModuleRecoveryRefV2,
    ) -> bool:
        try:
            capture = cls._operation_from_result(result)
            installed = cls._installed_result(result, recovery)
            if result.phase in {"restore-ready", "restored"}:
                expected = cls._restore_operation(capture, result)
                if operation != expected:
                    return False
            elif operation != capture:
                return False
            result.validate_for(operation)
            installed.validate_for(capture)
        except ValueError, CategorizedError:
            return False
        return (
            operation.system_id == recovery.system_id
            and operation.run_id == recovery.run_id
            and operation.operation_nonce == recovery.operation_nonce
            and identity_for(capture) == recovery.operation_identity
            and identity_for(installed) == recovery.result_identity
        )

    async def reopen_operation(
        self, recovery: RemoteModuleRecoveryRefV2
    ) -> RemoteModuleOperationV1:
        raw = await self.read_scratch_result(recovery)
        if raw is None:
            return (await self._evidence(recovery))[0]
        result = self._decode_scratch(raw)
        capture = self._operation_from_result(result)
        operation = (
            self._restore_operation(capture, result)
            if result.phase in {"restore-ready", "restored"}
            else capture
        )
        if not self._matches_recovery(operation, result, recovery):
            raise CategorizedError(
                "remote module scratch result differs from recovery reference",
                category=ErrorCategory.CONFLICT,
            )
        return operation

    async def reopen_capture_operation(
        self, recovery: RemoteModuleRecoveryRefV2
    ) -> RemoteModuleOperationV1:
        return self._operation_from_result(await self.reopen_installed_result(recovery))

    async def reopen_installed_result(
        self, recovery: RemoteModuleRecoveryRefV2
    ) -> RemoteModuleResultV1:
        result = await self.reopen_result(recovery)
        return self._installed_result(result, recovery)

    @staticmethod
    def _decode_scratch(raw: bytes) -> RemoteModuleResultV1:
        try:
            return RemoteModuleResultV1.from_wire_bytes(raw)
        except ValueError:
            raise CategorizedError(
                "remote module scratch result is invalid", category=ErrorCategory.CONFLICT
            ) from None

    async def reopen_result(self, recovery: RemoteModuleRecoveryRefV2) -> RemoteModuleResultV1:
        raw = await self.read_scratch_result(recovery)
        if raw is None:
            return (await self._evidence(recovery))[1]
        result = self._decode_scratch(raw)
        capture = self._operation_from_result(result)
        operation = (
            self._restore_operation(capture, result)
            if result.phase in {"restore-ready", "restored"}
            else capture
        )
        if not self._matches_recovery(operation, result, recovery):
            raise CategorizedError(
                "remote module scratch result differs from recovery reference",
                category=ErrorCategory.CONFLICT,
            )
        return result
