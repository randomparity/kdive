"""Application-facing ports for scheduling and querying durable jobs.

Services express lifecycle intent through these semantic operations. The jobs layer owns queue
calls, payload construction, and authorizing-context translation.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from psycopg import AsyncConnection

from kdive.domain.operations.jobs import Job, JobKind
from kdive.security.authz.context import RequestContext


class ProvisionJobPort(Protocol):
    """Provision admission's durable-job operations."""

    async def enqueue_provision(
        self,
        conn: AsyncConnection,
        ctx: RequestContext,
        *,
        project: str,
        allocation_id: UUID,
        system_id: UUID,
    ) -> Job: ...

    async def find_by_dedup_key(self, conn: AsyncConnection, dedup_key: str) -> Job | None: ...


class TeardownJobPort(Protocol):
    """Investigation lifecycle's forced-teardown operation."""

    async def enqueue_teardown(
        self,
        conn: AsyncConnection,
        ctx: RequestContext,
        *,
        project: str,
        system_id: UUID,
    ) -> Job: ...


class JobQueryPort(Protocol):
    """Read-side job evidence needed by Run services."""

    async def find_by_dedup_key(self, conn: AsyncConnection, dedup_key: str) -> Job | None: ...

    async def latest_succeeded_for_system(
        self, conn: AsyncConnection, kind: JobKind, system_id: UUID
    ) -> Job | None: ...
