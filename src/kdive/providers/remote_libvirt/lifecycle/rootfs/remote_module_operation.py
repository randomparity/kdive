"""Async durable-evidence runtime seam for remote module recovery (ADR-0588)."""

from __future__ import annotations

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
    """Reopen terminal state from the durable obligation row, not a journal volume."""

    conn: AsyncConnection
    repository: RemoteModuleAttemptObligationRepository

    @staticmethod
    def _attempt(recovery: RemoteModuleRecoveryRefV1) -> ModuleAttempt:
        return ModuleAttempt(
            UUID(recovery.system_id), UUID(recovery.run_id), recovery.operation_nonce
        )

    async def _evidence(self, recovery: RemoteModuleRecoveryRefV1):
        evidence = await self.repository.read_terminal_evidence(self.conn, self._attempt(recovery))
        if evidence is None:
            raise CategorizedError(
                "remote module terminal evidence is absent", category=ErrorCategory.CONFLICT
            )
        try:
            operation = RemoteModuleOperationV1.model_validate(evidence.terminal_operation)
            result = RemoteModuleResultV1.model_validate(evidence.terminal_result)
            stored = RemoteModuleRecoveryRefV1.model_validate(evidence.recovery_reference)
        except ValueError as exc:
            raise CategorizedError(
                "remote module terminal evidence is invalid", category=ErrorCategory.CONFLICT
            ) from exc
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

    async def reopen_operation(
        self, recovery: RemoteModuleRecoveryRefV1
    ) -> RemoteModuleOperationV1:
        return (await self._evidence(recovery))[0]

    async def reopen_result(self, recovery: RemoteModuleRecoveryRefV1) -> RemoteModuleResultV1:
        return (await self._evidence(recovery))[1]
