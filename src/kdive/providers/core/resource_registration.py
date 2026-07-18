"""Resource discovery registration service."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from psycopg import AsyncConnection, AsyncCursor
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.catalog.discovery import DiscoverySource, ResourceRecord
from kdive.domain.catalog.resource_capabilities import OPERATOR_OWNED_CAP_KEYS
from kdive.domain.catalog.resources import ManagedBy, Resource, ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory

_log = logging.getLogger(__name__)


async def register_discovered_resource(
    conn: AsyncConnection,
    record: ResourceRecord,
    *,
    pool: str,
    cost_class: str,
) -> Resource:
    """Upsert one discovered Resource by ``(kind, resource_id)``."""
    resource = _resource_from_record(record, pool=pool, cost_class=cost_class)
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.RESOURCE, _resource_key(resource)),
        conn.cursor(row_factory=dict_row) as cur,
    ):
        await cur.execute(
            "SELECT id FROM resources WHERE kind = %s AND host_uri = %s FOR UPDATE",
            (resource.kind.value, resource.host_uri),
        )
        existing = await cur.fetchone()
        if existing is not None:
            await cur.execute(
                "UPDATE resources SET capabilities = %s, status = %s, pool = %s, "
                "cost_class = %s WHERE id = %s RETURNING *",
                (
                    Jsonb(resource.capabilities),
                    resource.status.value,
                    resource.pool,
                    resource.cost_class,
                    existing["id"],
                ),
            )
        else:
            await _insert_resource(cur, resource)
        row = await cur.fetchone()
    if row is None:  # Invariant: INSERT/UPDATE ... RETURNING always yields one row.
        raise RuntimeError("resource registration returned no row")
    return Resource.model_validate(row)


async def register_or_refresh_discovered_resource(
    pool: AsyncConnectionPool,
    discovery: DiscoverySource,
    *,
    kind: ResourceKind,
    resource_id: str,
    pool_name: str,
    cost_class: str,
) -> None:
    """Insert the target discovered Resource when absent, else refresh its capabilities.

    On an existing row the refresh is discovery-authoritative except for operator-owned keys
    (``OPERATOR_OWNED_CAP_KEYS``), best-effort (a discovery-read failure keeps the stored
    capabilities), and change-guarded. The existence probe is ``SELECT ... FOR UPDATE`` so the
    read-modify-write serializes against ``ops.set_host_capacity`` on the row lock (ADR-0384).
    """
    async with (
        pool.connection() as conn,
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.RESOURCE, _resource_key(kind, resource_id)),
    ):
        stored = await _locked_capabilities(conn, kind, resource_id)
        if stored is not None:
            await _refresh_capabilities(
                conn, discovery, kind=kind, resource_id=resource_id, stored=stored
            )
            return
        records = await asyncio.to_thread(discovery.list_resources)
        record = _select_record(records, kind=kind, resource_id=resource_id)
        resource = _resource_from_record(record, pool=pool_name, cost_class=cost_class)
        async with conn.cursor(row_factory=dict_row) as cur:
            await _insert_resource(cur, resource)


async def _locked_capabilities(
    conn: AsyncConnection, kind: ResourceKind, resource_id: str
) -> dict[str, Any] | None:
    """Lock the row ``FOR UPDATE`` and return its capabilities, or ``None`` when it is absent."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT capabilities FROM resources WHERE kind = %s AND host_uri = %s FOR UPDATE",
            (kind.value, resource_id),
        )
        row = await cur.fetchone()
    return row["capabilities"] if row is not None else None


async def _refresh_capabilities(
    conn: AsyncConnection,
    discovery: DiscoverySource,
    *,
    kind: ResourceKind,
    resource_id: str,
    stored: dict[str, Any],
) -> None:
    """Best-effort refresh of an existing (already ``FOR UPDATE``-locked) row's capabilities.

    The ``try`` wraps only the discovery read + record selection; a failure logs and keeps the
    stored capabilities. The change-guarded ``UPDATE`` runs outside the catch, so a genuine DB
    write error propagates rather than poisoning the outer transaction.
    """
    try:
        records = await asyncio.to_thread(discovery.list_resources)
        record = _select_record(records, kind=kind, resource_id=resource_id)
    except Exception:  # noqa: BLE001 - best-effort refresh keeps the existing row on any read fault
        _log.warning(
            "capability refresh skipped for %s:%s; keeping existing capabilities",
            kind.value,
            resource_id,
            exc_info=True,
        )
        return
    merged = _merge_capabilities(record["capabilities"], stored)
    if merged == stored:
        return
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE resources SET capabilities = %s WHERE kind = %s AND host_uri = %s",
            (Jsonb(merged), kind.value, resource_id),
        )


def _merge_capabilities(fresh: dict[str, Any], stored: dict[str, Any]) -> dict[str, Any]:
    """Discovery-authoritative merge that overlays operator-owned keys from the stored row."""
    merged = dict(fresh)
    for key in OPERATOR_OWNED_CAP_KEYS:
        if key in stored:
            merged[key] = stored[key]
    return merged


def _select_record(
    records: list[ResourceRecord], *, kind: ResourceKind, resource_id: str
) -> ResourceRecord:
    for record in records:
        if record["kind"] == kind and record["resource_id"] == resource_id:
            return record
    raise CategorizedError(
        "discovery source did not return the requested resource",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"kind": kind.value, "resource_id": resource_id},
    )


def _resource_from_record(record: ResourceRecord, *, pool: str, cost_class: str) -> Resource:
    now = datetime.now(UTC)
    return Resource(
        id=uuid4(),
        created_at=now,
        updated_at=now,
        kind=record["kind"],
        capabilities=record["capabilities"],
        pool=pool,
        cost_class=cost_class,
        status=record["status"],
        host_uri=record["resource_id"],
        # A host discovered AFTER migration 0030 must insert at 'discovery', not the column
        # default 'runtime' (ADR-0112 invariant 5) — the migration only backfilled rows that
        # existed at migrate time, so the insert path owns the ownership label going forward.
        managed_by=ManagedBy.DISCOVERY,
    )


def _resource_key(kind: ResourceKind | Resource, resource_id: str | None = None) -> str:
    if isinstance(kind, Resource):
        return f"{kind.kind.value}:{kind.host_uri}"
    if resource_id is None:
        raise ValueError("resource_id is required when kind is not a Resource")
    return f"{kind.value}:{resource_id}"


async def _insert_resource(cur: AsyncCursor[dict[str, Any]], resource: Resource) -> None:
    await cur.execute(
        """
        INSERT INTO resources
            (id, kind, capabilities, pool, cost_class, status, host_uri, managed_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            resource.id,
            resource.kind.value,
            Jsonb(resource.capabilities),
            resource.pool,
            resource.cost_class,
            resource.status.value,
            resource.host_uri,
            resource.managed_by.value,
        ),
    )
