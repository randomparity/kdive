"""Jobs-layer implementation of application-service job ports."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from psycopg import AsyncConnection

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import SYSTEMS, ObjectNotFound
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import queue
from kdive.jobs.context import authorizing
from kdive.jobs.payloads import SystemPayload
from kdive.providers.system_authority.protocol import AuthoritySystemMarkerV1
from kdive.security.authz.context import RequestContext
from kdive.services.systems.authority_owned import enqueue_control_teardown


@dataclass(frozen=True, slots=True)
class JobOperations:
    """Translate service-level lifecycle intent into queue records."""

    async def enqueue_provision(
        self,
        conn: AsyncConnection,
        ctx: RequestContext,
        *,
        project: str,
        allocation_id: UUID,
        system_id: UUID,
        authority_marker: AuthoritySystemMarkerV1 | None = None,
    ) -> Job:
        return await queue.enqueue(
            conn,
            JobKind.PROVISION,
            SystemPayload(system_id=str(system_id), authority_system_v1=authority_marker),
            authorizing(ctx, project),
            f"{allocation_id}:provision",
        )

    async def enqueue_teardown(
        self,
        conn: AsyncConnection,
        ctx: RequestContext,
        *,
        project: str,
        system_id: UUID,
    ) -> Job:
        async with advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
            system = await SYSTEMS.get(conn, system_id)
            if system is None:
                raise ObjectNotFound("system", system_id)
            return await enqueue_control_teardown(conn, system, authorizing(ctx, project))

    async def find_by_dedup_key(self, conn: AsyncConnection, dedup_key: str) -> Job | None:
        return await queue.get_by_dedup_key(conn, dedup_key)

    async def latest_succeeded_for_system(
        self, conn: AsyncConnection, kind: JobKind, system_id: UUID
    ) -> Job | None:
        return await queue.latest_succeeded_job_for_system(conn, kind, system_id)
