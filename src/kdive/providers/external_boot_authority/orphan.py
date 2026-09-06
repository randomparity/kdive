"""Authority-only disposition of durable external-boot quarantine selections (#2204)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.providers.external_boot_authority.protocol import (
    AuthorityRecoveryOrphanDispositionRequestV1,
    AuthorityRecoveryOrphanDispositionResponseV1,
)
from kdive.providers.external_boot_authority.service import AuthenticatedPeer, AuthorityServiceError
from kdive.providers.ports.external_boot import (
    ExternalBootActivationBinding,
    ExternalBootRecoveryObjectPorts,
    OpaqueProviderRef,
    RecoveryObjectBinding,
    RecoveryObjectObservation,
)


class AuthorityConnectionFactory(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[AsyncConnection]: ...


class RecoveryObjectExecutor(Protocol):
    """Bounded provider-host executor for private recovery-object operations."""

    async def observe_recovery_object(
        self, binding: RecoveryObjectBinding, authority: OpaqueProviderRef
    ) -> RecoveryObjectObservation: ...

    async def delete_recovery_object(
        self, binding: RecoveryObjectBinding, authority: OpaqueProviderRef, digest: str
    ) -> RecoveryObjectObservation: ...

    async def adopt_recovery_object(
        self, binding: RecoveryObjectBinding, authority: OpaqueProviderRef, digest: str
    ) -> RecoveryObjectObservation: ...


@dataclass(frozen=True, slots=True)
class _Selection:
    object_id: UUID
    system_id: UUID
    activation_id: UUID
    run_id: UUID
    disposition: Literal["delete", "adopt"]
    authority_instance: str
    kind: Literal["kernel", "initrd", "modules", "recovery-record"]
    reference: str
    ownership_digest: str
    observed_digest: str
    operation_identity: str
    attempt_id: UUID
    journal_sequence: int
    journal_digest: str
    reserved_bytes: int
    deadline: datetime

    def binding(self) -> RecoveryObjectBinding:
        return RecoveryObjectBinding(
            record_id=str(self.object_id),
            binding=ExternalBootActivationBinding(
                system_id=str(self.system_id),
                run_id=str(self.run_id),
                activation_id=str(self.activation_id),
            ),
            kind=self.kind,
            reference=OpaqueProviderRef(ref=self.reference),
            ownership_digest=self.ownership_digest,
            operation_identity=self.operation_identity,
            attempt_id=str(self.attempt_id),
            mutation_journal_sequence=self.journal_sequence,
            mutation_journal_digest=self.journal_digest,
            reserved_bytes=self.reserved_bytes,
        )


class RecoveryOrphanAuthorityService:
    """Resolve the immutable selection before each private provider disposition."""

    def __init__(
        self,
        connections: AuthorityConnectionFactory,
        ports: ExternalBootRecoveryObjectPorts,
        executor: RecoveryObjectExecutor | None = None,
    ) -> None:
        self._connections = connections
        self._ports = ports
        self._executor = executor
        self._lanes: dict[UUID, asyncio.Lock] = {}
        self._serializer: (
            Callable[
                [UUID, Callable[[], Awaitable[AuthorityRecoveryOrphanDispositionResponseV1]]],
                Awaitable[AuthorityRecoveryOrphanDispositionResponseV1],
            ]
            | None
        ) = None

    def set_serializer(
        self,
        serializer: Callable[
            [UUID, Callable[[], Awaitable[AuthorityRecoveryOrphanDispositionResponseV1]]],
            Awaitable[AuthorityRecoveryOrphanDispositionResponseV1],
        ],
    ) -> None:
        """Use the mutation service's System lane when both routes share a host."""
        self._serializer = serializer

    async def _selection(
        self, peer: AuthenticatedPeer, request: AuthorityRecoveryOrphanDispositionRequestV1
    ) -> tuple[_Selection, ...]:
        async with self._connections() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT verify_external_boot_recovery_orphan_inventory_authority(%s,%s,%s,%s)",
                (
                    str(peer.incarnation_id),
                    request.request_id,
                    request.job_id,
                    request.job_attempt,
                ),
            )
            await cur.execute(
                "SELECT * FROM resolve_external_boot_recovery_orphan_authority(%s,%s,%s,%s)",
                (
                    str(peer.incarnation_id),
                    request.request_id,
                    request.job_id,
                    request.job_attempt,
                ),
            )
            rows = await cur.fetchall()
        if not rows:
            raise AuthorityServiceError("superseded")
        try:
            return tuple(
                _Selection(
                    object_id=row["object_id"],
                    system_id=row["system_id"],
                    activation_id=row["activation_id"],
                    run_id=row["run_id"],
                    disposition=row["disposition"],
                    authority_instance=row["authority_instance"],
                    kind=row["object_kind"],
                    reference=row["object_reference"],
                    ownership_digest=row["ownership_digest"],
                    observed_digest=row["observed_digest"],
                    operation_identity=row["operation_identity"],
                    attempt_id=row["attempt_id"],
                    journal_sequence=row["mutation_journal_sequence"],
                    journal_digest=row["mutation_journal_digest"],
                    reserved_bytes=row["reserved_bytes"],
                    deadline=row["readiness_deadline"],
                )
                for row in rows
            )
        except KeyError, TypeError, ValueError:
            raise AuthorityServiceError("journal_conflict") from None

    async def _commit(
        self,
        peer: AuthenticatedPeer,
        request: AuthorityRecoveryOrphanDispositionRequestV1,
        selection: _Selection,
        observed_digest: str,
    ) -> None:
        async with self._connections() as conn:
            row = await (
                await conn.execute(
                    "SELECT commit_external_boot_recovery_orphan_disposition(%s,%s,%s,%s,%s,%s,%s)",
                    (
                        str(peer.incarnation_id),
                        request.request_id,
                        request.job_id,
                        request.job_attempt,
                        selection.object_id,
                        selection.ownership_digest,
                        observed_digest,
                    ),
                )
            ).fetchone()
        if row is None or row[0] != "applied":
            raise AuthorityServiceError("superseded")

    async def _observe(
        self, binding: RecoveryObjectBinding, authority: OpaqueProviderRef
    ) -> RecoveryObjectObservation:
        if self._executor is not None:
            return await self._executor.observe_recovery_object(binding, authority)
        return self._ports.observe_object(binding, authority)

    async def _disposition(
        self,
        selection: _Selection,
        binding: RecoveryObjectBinding,
        authority: OpaqueProviderRef,
    ) -> RecoveryObjectObservation:
        if self._executor is not None:
            if selection.disposition == "delete":
                return await self._executor.delete_recovery_object(
                    binding, authority, selection.observed_digest
                )
            return await self._executor.adopt_recovery_object(
                binding, authority, selection.observed_digest
            )
        if selection.disposition == "delete":
            return self._ports.delete_recovery_object(binding, authority, selection.observed_digest)
        return self._ports.adopt_object(binding, authority, selection.observed_digest)

    async def resolve_recovery_orphan(
        self, peer: AuthenticatedPeer, request: AuthorityRecoveryOrphanDispositionRequestV1
    ) -> AuthorityRecoveryOrphanDispositionResponseV1:
        selections = await self._selection(peer, request)
        system_id = selections[0].system_id
        if any(selection.system_id != system_id for selection in selections):
            raise AuthorityServiceError("journal_conflict")

        async def resolve_selected() -> AuthorityRecoveryOrphanDispositionResponseV1:
            selections = await self._selection(peer, request)
            completed = 0
            for selection in selections:
                now = datetime.now(selection.deadline.tzinfo)
                if now >= selection.deadline:
                    raise AuthorityServiceError("superseded")
                binding = selection.binding()
                authority = OpaqueProviderRef(ref=selection.authority_instance)
                observed = await self._observe(binding, authority)
                if observed.binding != binding:
                    raise AuthorityServiceError("journal_conflict")
                done = (selection.disposition == "delete" and not observed.present) or (
                    selection.disposition == "adopt" and observed.present and observed.managed
                )
                if not done:
                    if observed.observed_digest != selection.observed_digest:
                        raise AuthorityServiceError("journal_conflict")
                    observed = await self._disposition(selection, binding, authority)
                    if observed.binding != binding:
                        raise AuthorityServiceError("journal_conflict")
                if (selection.disposition == "delete" and observed.present) or (
                    selection.disposition == "adopt"
                    and (not observed.present or not observed.managed)
                ):
                    raise AuthorityServiceError("provider_conflict")
                await self._commit(peer, request, selection, observed.observed_digest)
                completed += 1
            return AuthorityRecoveryOrphanDispositionResponseV1(
                request_id=request.request_id,
                disposition=selections[0].disposition,
                objects=completed,
            )

        if self._serializer is not None:
            return await self._serializer(system_id, resolve_selected)
        lane = self._lanes.setdefault(system_id, asyncio.Lock())
        async with lane:
            return await resolve_selected()
