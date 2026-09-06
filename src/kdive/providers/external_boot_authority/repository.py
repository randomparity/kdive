"""Short-lived database adapter for the external-boot authority service (ADR-0584)."""

from __future__ import annotations

import hashlib
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from typing import Protocol

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from kdive.db.external_boot_authority_journal import (
    AdvanceStatus,
    AuthorityBinding,
    JournalHead,
    advance_journal_head,
    read_journal_head,
    resolve_allocating_authority_binding,
    resolve_current_authority_binding,
    resolve_current_authority_candidate,
    resolve_current_preparation_authority_binding,
    resolve_current_release_phase_authority_binding,
)
from kdive.providers.external_boot_authority.journal import record_digest
from kdive.providers.external_boot_authority.protocol import (
    AuthorityAcknowledgementV1,
    AuthorityMutationRequestV1,
    AuthorityPreparationMutationRequestV1,
    AuthorityTakeoverRequestV1,
    JournalRecordV1,
)
from kdive.providers.external_boot_authority.service import AuthenticatedPeer
from kdive.providers.ports.external_boot import ExternalBootPlan, RecoveryObjectObservation


class AuthorityConnectionFactory(Protocol):
    """Open one already validated authority-role database connection."""

    def __call__(self) -> AbstractAsyncContextManager[AsyncConnection]: ...


class DatabaseAuthorityRepository:
    """Adapt trusted SQL functions without retaining connections between calls."""

    def __init__(self, connections: AuthorityConnectionFactory) -> None:
        self._connections = connections

    async def resolve_allocating(
        self, peer: AuthenticatedPeer, request: AuthorityTakeoverRequestV1
    ) -> AuthorityBinding | None:
        async with self._connections() as conn, conn.transaction():
            binding = await resolve_allocating_authority_binding(
                conn,
                peer_incarnation_id=str(peer.incarnation_id),
                authority_id=request.authority_id,
                generation=request.generation,
            )
            if binding is None or binding.purpose != "activate":
                return binding
            row = await conn.execute(
                "SELECT resolve_allocating_external_boot_preparation_plan(%s,%s,%s)",
                (str(peer.incarnation_id), request.authority_id, request.generation),
            )
            plan = await row.fetchone()
            if plan is None or plan[0] is None:
                return binding
            return replace(binding, preparation_plan=ExternalBootPlan.model_validate(plan[0]))

    async def resolve_current(
        self,
        peer: AuthenticatedPeer,
        request: AuthorityMutationRequestV1,
        acknowledgement_sequence: int,
        acknowledgement_digest: str,
    ) -> AuthorityBinding | None:
        async with self._connections() as conn, conn.transaction():
            return await resolve_current_authority_binding(
                conn,
                peer_incarnation_id=str(peer.incarnation_id),
                authority_id=request.authority_id,
                generation=request.generation,
                acknowledgement_sequence=acknowledgement_sequence,
                acknowledgement_digest=acknowledgement_digest,
            )

    async def resolve_current_candidate(
        self, peer: AuthenticatedPeer, request: AuthorityMutationRequestV1
    ) -> AuthorityBinding | None:
        async with self._connections() as conn, conn.transaction():
            return await resolve_current_authority_candidate(
                conn,
                peer_incarnation_id=str(peer.incarnation_id),
                authority_id=request.authority_id,
                generation=request.generation,
            )

    async def resolve_current_preparation(
        self,
        peer: AuthenticatedPeer,
        request: AuthorityPreparationMutationRequestV1,
        acknowledgement_sequence: int,
        acknowledgement_digest: str,
    ) -> AuthorityBinding | None:
        operation = request.operation.value
        if operation not in {"materialize", "prepare"}:
            raise ValueError("preparation authority operation must be materialize or prepare")
        async with self._connections() as conn, conn.transaction():
            return await resolve_current_preparation_authority_binding(
                conn,
                peer_incarnation_id=str(peer.incarnation_id),
                authority_id=request.authority_id,
                generation=request.generation,
                acknowledgement_sequence=acknowledgement_sequence,
                acknowledgement_digest=acknowledgement_digest,
                operation=operation,
            )

    async def resolve_current_release_phase(
        self,
        peer: AuthenticatedPeer,
        request: AuthorityMutationRequestV1,
        acknowledgement_sequence: int,
        acknowledgement_digest: str,
    ) -> AuthorityBinding | None:
        operation = request.operation.value
        if operation not in {"recover", "cleanup"}:
            raise ValueError("release phase operation must be recover or cleanup")
        async with self._connections() as conn, conn.transaction():
            return await resolve_current_release_phase_authority_binding(
                conn,
                peer_incarnation_id=str(peer.incarnation_id),
                authority_id=request.authority_id,
                generation=request.generation,
                acknowledgement_sequence=acknowledgement_sequence,
                acknowledgement_digest=acknowledgement_digest,
                operation=operation,
            )

    async def read_head(self, binding: AuthorityBinding) -> JournalHead | None:
        async with self._connections() as conn, conn.transaction():
            return await read_journal_head(conn, binding=binding)

    async def acknowledge(
        self,
        peer: AuthenticatedPeer,
        binding: AuthorityBinding,
        request: AuthorityTakeoverRequestV1,
        acknowledgement: AuthorityAcknowledgementV1,
    ) -> AuthorityAcknowledgementV1 | None:
        """Project an exact journal acknowledgement into the trusted core transaction."""
        if (
            binding.peer_incarnation_id != str(peer.incarnation_id)
            or binding.authority_id != request.authority_id
            or binding.generation != request.generation
            or binding.system_id != request.system_id
            or binding.activation_id != request.activation_id
            or binding.run_id != request.run_id
            or binding.plan_identity != request.plan_identity
            or binding.purpose != request.purpose
            or binding.operation != request.operation
            or binding.provider_kind != request.provider_kind
            or binding.authority_instance != request.authority_instance
            or binding.operation_identity != request.operation_identity
            or binding.operation_digest != request.operation_digest
            or acknowledgement.authority_id != request.authority_id
            or acknowledgement.generation != request.generation
            or acknowledgement.system_id != request.system_id
        ):
            return None
        async with self._connections() as conn, conn.transaction():
            cursor = await conn.execute(
                "SELECT allocation_id, job_id, job_attempt, worker_incarnation "
                "FROM external_boot_authorities WHERE id = %s",
                (binding.authority_id,),
            )
            authority = await cursor.fetchone()
            if authority is None or str(authority[3]) != str(peer.incarnation_id):
                return None
            cursor = await conn.execute(
                "SELECT status, journal_sequence, journal_digest, "
                "positive_quiescence_digest FROM acknowledge_external_boot_authority("
                "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    request.authority_id,
                    request.generation,
                    authority[0],
                    request.activation_id,
                    request.run_id,
                    request.system_id,
                    request.plan_identity,
                    authority[1],
                    authority[2],
                    request.purpose,
                    request.provider_kind,
                    request.authority_instance,
                    str(peer.incarnation_id),
                    request.operation.value,
                    request.operation_identity,
                    request.operation_digest,
                    acknowledgement.journal_sequence,
                    acknowledgement.journal_digest,
                    acknowledgement.positive_quiescence_digest,
                ),
            )
            result = await cursor.fetchone()
            if result is None or result[0] != "applied":
                return None
            return AuthorityAcknowledgementV1(
                authority_id=request.authority_id,
                generation=request.generation,
                system_id=request.system_id,
                journal_sequence=result[1],
                journal_digest=result[2],
                positive_quiescence_digest=result[3],
            )

    async def advance(
        self,
        binding: AuthorityBinding,
        expected_sequence: int,
        expected_digest: str,
        record: JournalRecordV1,
    ) -> AdvanceStatus:
        async with self._connections() as conn, conn.transaction():
            return await advance_journal_head(
                conn,
                binding=binding,
                expected_sequence=expected_sequence,
                expected_digest=expected_digest,
                record=record,
            )

    async def publish_cleanup_quarantine(
        self,
        peer: AuthenticatedPeer,
        binding: AuthorityBinding,
        terminal: JournalRecordV1,
        observations: tuple[RecoveryObjectObservation, ...],
    ) -> None:
        """Publish bounded private receipt evidence only under the current terminal authority."""
        if terminal.system_id != binding.system_id or terminal.phase.value != "terminal":
            raise ValueError("cleanup quarantine terminal record does not match authority")
        objects = []
        for observation in observations:
            object_binding = observation.binding
            if (
                not observation.present
                or observation.managed
                or object_binding.binding.system_id != str(binding.system_id)
                or object_binding.binding.activation_id != str(binding.activation_id)
                or object_binding.binding.run_id != str(binding.run_id)
                or object_binding.operation_identity != binding.operation_identity
            ):
                raise ValueError("cleanup quarantine receipt does not match current authority")
            identity = (
                "sha256:"
                + hashlib.sha256(
                    (
                        "kdive-recovery-quarantine-object-v1\0"
                        f"{object_binding.record_id}\0{object_binding.reference.ref}\0"
                        f"{object_binding.ownership_digest}\0{object_binding.mutation_journal_digest}"
                    ).encode()
                ).hexdigest()
            )
            objects.append(
                {
                    "id": str(object_binding.record_id),
                    "object_identity": identity,
                    "object_kind": object_binding.kind,
                    "object_reference": object_binding.reference.ref,
                    "ownership_digest": object_binding.ownership_digest,
                    "observed_digest": observation.observed_digest,
                    "attempt_id": str(object_binding.attempt_id),
                    "mutation_journal_sequence": object_binding.mutation_journal_sequence,
                    "mutation_journal_digest": object_binding.mutation_journal_digest,
                    "reserved_bytes": object_binding.reserved_bytes,
                }
            )
        async with self._connections() as conn, conn.transaction():
            row = await conn.execute(
                "SELECT publish_external_boot_recovery_quarantine_authority(%s,%s,%s,%s,%s,%s,%s)",
                (
                    str(peer.incarnation_id),
                    binding.authority_id,
                    binding.generation,
                    binding.operation_identity,
                    terminal.sequence,
                    record_digest(terminal),
                    Jsonb(objects),
                ),
            )
            result = await row.fetchone()
        if result is None or result[0] != len(objects):
            raise RuntimeError("cleanup quarantine publication was not applied")
