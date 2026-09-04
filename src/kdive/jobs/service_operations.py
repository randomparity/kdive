"""Jobs-layer implementation of application-service job ports."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from psycopg import AsyncConnection

from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import queue
from kdive.jobs.context import authorizing
from kdive.jobs.payloads import SystemPayload, TeardownPayload
from kdive.security.authz.context import RequestContext


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
    ) -> Job:
        return await queue.enqueue(
            conn,
            JobKind.PROVISION,
            SystemPayload(system_id=str(system_id)),
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
        return await queue.enqueue(
            conn,
            JobKind.TEARDOWN,
            TeardownPayload(system_id=str(system_id)),
            authorizing(ctx, project),
            f"{system_id}:teardown",
        )

    async def find_by_dedup_key(self, conn: AsyncConnection, dedup_key: str) -> Job | None:
        return await queue.get_by_dedup_key(conn, dedup_key)

    async def latest_succeeded_for_system(
        self, conn: AsyncConnection, kind: JobKind, system_id: UUID
    ) -> Job | None:
        return await queue.latest_succeeded_job_for_system(conn, kind, system_id)
