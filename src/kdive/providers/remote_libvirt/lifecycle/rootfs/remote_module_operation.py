"""Async durable-evidence runtime seam for remote module recovery (ADR-0588)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from kdive.db.remote_module_attempt_obligations import (
    ModuleAttempt,
    RemoteModuleAttemptObligationRepository,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleOperationV1,
    RemoteModuleRecoveryRefV1,
    RemoteModuleResultV1,
    identity_for,
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


@dataclass(frozen=True, slots=True)
class RemoteModuleOperationRuntime:
    """Reopen scratch state first, falling back to durable evidence after scratch deletion."""

    pool: AsyncConnectionPool
    repository: RemoteModuleAttemptObligationRepository
    read_scratch_result: Callable[[RemoteModuleRecoveryRefV1], Awaitable[bytes | None]]

    @staticmethod
    def _attempt(recovery: RemoteModuleRecoveryRefV1) -> ModuleAttempt:
        return ModuleAttempt(
            UUID(recovery.system_id), UUID(recovery.run_id), recovery.operation_nonce
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
