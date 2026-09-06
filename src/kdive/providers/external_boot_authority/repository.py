"""Short-lived database adapter for the external-boot authority service (ADR-0584)."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Protocol

from psycopg import AsyncConnection

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
)
from kdive.providers.external_boot_authority.protocol import (
    AuthorityMutationRequestV1,
    AuthorityPreparationMutationRequestV1,
    AuthorityTakeoverRequestV1,
    JournalRecordV1,
)
from kdive.providers.external_boot_authority.service import AuthenticatedPeer


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
            return await resolve_allocating_authority_binding(
                conn,
                peer_incarnation_id=str(peer.incarnation_id),
                authority_id=request.authority_id,
                generation=request.generation,
            )

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

    async def read_head(self, binding: AuthorityBinding) -> JournalHead | None:
        async with self._connections() as conn, conn.transaction():
            return await read_journal_head(conn, binding=binding)

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
