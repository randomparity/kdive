"""Async durable-evidence runtime seam for remote module recovery (ADR-0588)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from psycopg import AsyncConnection

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


@dataclass(frozen=True, slots=True)
class RemoteModuleOperationRuntime:
    """Reopen scratch state first, falling back to durable evidence after scratch deletion."""

    conn: AsyncConnection
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
        evidence = await self.repository.read_terminal_evidence(self.conn, self._attempt(recovery))
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
        if (
            stored != recovery
            or identity_for(operation) != evidence.terminal_operation_identity
            or identity_for(result) != evidence.terminal_result_identity
            or operation.system_id != recovery.system_id
            or operation.run_id != recovery.run_id
            or operation.operation_nonce != recovery.operation_nonce
        ):
            raise CategorizedError(
                "remote module terminal evidence differs from recovery reference",
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

    async def reopen_operation(
        self, recovery: RemoteModuleRecoveryRefV1
    ) -> RemoteModuleOperationV1:
        result = await self.reopen_result(recovery)
        return self._operation_from_result(result)

    async def reopen_result(self, recovery: RemoteModuleRecoveryRefV1) -> RemoteModuleResultV1:
        raw = await self.read_scratch_result(recovery)
        if raw is None:
            return (await self._evidence(recovery))[1]
        try:
            result = RemoteModuleResultV1.from_wire_bytes(raw)
        except ValueError:
            raise CategorizedError(
                "remote module scratch result is invalid", category=ErrorCategory.CONFLICT
            ) from None
        if identity_for(result) != recovery.result_identity:
            raise CategorizedError(
                "remote module scratch result differs from recovery reference",
                category=ErrorCategory.CONFLICT,
            )
        return result
