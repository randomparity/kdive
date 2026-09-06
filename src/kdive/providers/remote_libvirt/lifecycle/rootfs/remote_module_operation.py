"""Async durable-evidence runtime seam for remote module recovery (ADR-0588)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from kdive.db.remote_module_attempt_obligations import (
    ModuleAttempt,
    ModuleAttemptTerminalEvidence,
    RemoteModuleAttemptObligationRepository,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.remote_module_attempt_preparation import ModuleAttemptPreparationRequestV1
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
    RemoteModuleRecoveryRefV1,
    RemoteModuleResultV1,
    identity_for,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_preparation import (
    RemoteModulePreparationExecutor,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volumes import (
    FilesystemImageWriter,
    ModuleTreeEntry,
    PreparedModuleVolumes,
    PreparedVolume,
    StorageConn,
    VolumeRequest,
    delete_owned_attempt_volume,
    expected_attempt_volumes,
    prepare_attempt_volumes,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.remote_module_volume_preparation import (
    prepare_verified_remote_module_attempt,
)


class ModuleOperationRuntime(Protocol):
    """The phase-facing asynchronous recovery surface; it never reads libvirt metadata."""

    async def reopen_operation(
        self, recovery: RemoteModuleRecoveryRefV1
    ) -> RemoteModuleOperationV1: ...
    async def reopen_result(self, recovery: RemoteModuleRecoveryRefV1) -> RemoteModuleResultV1: ...
    async def reopen_capture_operation(
        self, recovery: RemoteModuleRecoveryRefV1
    ) -> RemoteModuleOperationV1: ...
    async def reopen_installed_result(
        self, recovery: RemoteModuleRecoveryRefV1
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
        recovery: RemoteModuleRecoveryRefV1,
        executor: RemoteModulePreparationExecutor,
        deadline: float,
    ) -> TeardownObservation: ...
    async def delete_source(
        self, recovery: RemoteModuleRecoveryRefV1, executor: RemoteModulePreparationExecutor
    ) -> None: ...
    async def delete_scratch(
        self, recovery: RemoteModuleRecoveryRefV1, executor: RemoteModulePreparationExecutor
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class RemoteModuleVolumePreparation:
    storage: StorageConn
    pool_name: str
    entries: tuple[ModuleTreeEntry, ...]
    writer: FilesystemImageWriter
    inspect_attachments: Callable[[], AttachmentInspection]
    work_dir: Path


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
    read_scratch_result: Callable[[RemoteModuleRecoveryRefV1], Awaitable[bytes | None]]
    volume_preparation: RemoteModuleVolumePreparation | None = None
    appliance_execution: RemoteModuleApplianceExecution | None = None

    @staticmethod
    def _attempt(recovery: RemoteModuleRecoveryRefV1) -> ModuleAttempt:
        return ModuleAttempt(
            UUID(recovery.system_id), UUID(recovery.run_id), recovery.operation_nonce
        )

    def _volume_request(self, operation: RemoteModuleOperationV1) -> VolumeRequest:
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
            inspect_attachments=configured.inspect_attachments,
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
            del identity
            if attempt != expected_attempt:
                raise CategorizedError(
                    "remote module verified attempt differs from operation",
                    category=ErrorCategory.CONFLICT,
                )
            volume_request = self._volume_request(operation)
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
        self, recovery: RemoteModuleRecoveryRefV1
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
            stored = RemoteModuleRecoveryRefV1.model_validate(evidence.recovery_reference)
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
            request = self._appliance_request(operation, volumes, deadline)
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
                durable = RemoteModuleResultV1.from_canonical_json(raw)
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

    def _appliance_request(
        self,
        operation: RemoteModuleOperationV1,
        volumes: PreparedModuleVolumes,
        deadline: float,
    ) -> ApplianceRequest:
        configured = self.appliance_execution
        prepared = self.volume_preparation
        if configured is None or prepared is None:
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
            pool=prepared.pool_name,
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
        recovery: RemoteModuleRecoveryRefV1,
        executor: RemoteModulePreparationExecutor,
        deadline: float,
    ) -> TeardownObservation:
        operation = await self.reopen_operation(recovery)
        volumes = expected_attempt_volumes(self._volume_request(operation))
        request = self._appliance_request(operation, volumes, deadline)
        configured = self.appliance_execution
        assert configured is not None
        return await executor.run(
            lambda: teardown_remote_module_appliance(configured.appliance, request)
        )

    async def _open_reap_evidence(self, recovery: RemoteModuleRecoveryRefV1) -> None:
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
        recovery: RemoteModuleRecoveryRefV1,
        executor: RemoteModulePreparationExecutor,
        purpose: str,
    ) -> None:
        if purpose == "scratch":
            await self._open_reap_evidence(recovery)
        operation = await self.reopen_operation(recovery)
        request = self._volume_request(operation)
        volumes = expected_attempt_volumes(request)
        selected = volumes.source if purpose == "source" else volumes.scratch
        configured = self.volume_preparation
        assert configured is not None

        def delete() -> None:
            inspection = configured.inspect_attachments()
            delete_owned_attempt_volume(configured.storage, selected, inspection=inspection)

        await executor.run(delete)

    async def delete_source(
        self, recovery: RemoteModuleRecoveryRefV1, executor: RemoteModulePreparationExecutor
    ) -> None:
        await self._delete(recovery, executor, "source")

    async def delete_scratch(
        self, recovery: RemoteModuleRecoveryRefV1, executor: RemoteModulePreparationExecutor
    ) -> None:
        await self._delete(recovery, executor, "scratch")

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
        result: RemoteModuleResultV1, recovery: RemoteModuleRecoveryRefV1
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
        recovery: RemoteModuleRecoveryRefV1,
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
        self, recovery: RemoteModuleRecoveryRefV1
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
        self, recovery: RemoteModuleRecoveryRefV1
    ) -> RemoteModuleOperationV1:
        return self._operation_from_result(await self.reopen_installed_result(recovery))

    async def reopen_installed_result(
        self, recovery: RemoteModuleRecoveryRefV1
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

    async def reopen_result(self, recovery: RemoteModuleRecoveryRefV1) -> RemoteModuleResultV1:
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
