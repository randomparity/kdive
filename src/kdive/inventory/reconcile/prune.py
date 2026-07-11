"""Inventory reconcile prune and cordon helpers."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg import AsyncConnection, AsyncCursor
from psycopg.rows import dict_row

from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.lifecycle.rules import NON_TERMINAL_ALLOCATION_STATE_VALUES
from kdive.images.cataloging.read_model import image_referenced_by_live_system
from kdive.inventory.reconcile.locks import resource_identity_lock
from kdive.inventory.reconcile.records import CONFIG_MANAGED_BY, PruneOutcome


async def prune_or_cordon_image(conn: AsyncConnection, row_id: UUID) -> PruneOutcome:
    """Apply the non-destructive prune contract to one config image row."""
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id FROM image_catalog WHERE id = %s AND managed_by = %s FOR UPDATE",
            (row_id, CONFIG_MANAGED_BY),
        )
        if await cur.fetchone() is None:
            return PruneOutcome(pruned=False, cordoned=False)
        if await image_referenced_by_live_system(cur, row_id):
            return PruneOutcome(pruned=False, cordoned=True)
        await cur.execute("DELETE FROM image_catalog WHERE id = %s", (row_id,))
    return PruneOutcome(pruned=True, cordoned=False)


async def prune_or_cordon_resource(
    conn: AsyncConnection, row_id: UUID, name: str, *, kind: ResourceKind
) -> PruneOutcome:
    """Apply the non-destructive prune contract to one config resource row."""
    async with (
        conn.transaction(),
        resource_identity_lock(conn, kind, name),
        conn.cursor(row_factory=dict_row) as cur,
    ):
        await cur.execute(
            "SELECT id FROM resources WHERE id = %s AND managed_by = %s FOR UPDATE",
            (row_id, CONFIG_MANAGED_BY),
        )
        if await cur.fetchone() is None:
            return PruneOutcome(pruned=False, cordoned=False)
        if await _resource_has_live_allocation(cur, row_id):
            await cur.execute(
                "UPDATE resources SET cordoned = true WHERE id = %s AND NOT cordoned", (row_id,)
            )
            return PruneOutcome(pruned=False, cordoned=True)
        await cur.execute("DELETE FROM resources WHERE id = %s", (row_id,))
    return PruneOutcome(pruned=True, cordoned=False)


async def _resource_has_live_allocation(cur: AsyncCursor[dict[str, Any]], row_id: UUID) -> bool:
    """True when the resource backs a non-terminal allocation."""
    await cur.execute(
        "SELECT 1 FROM allocations WHERE resource_id = %s AND state = ANY(%s) LIMIT 1",
        (row_id, list(NON_TERMINAL_ALLOCATION_STATE_VALUES)),
    )
    return await cur.fetchone() is not None


async def prune_or_cordon_removed_resource(
    conn: AsyncConnection, row_id: UUID, name: str, *, kind: ResourceKind
) -> PruneOutcome:
    """Apply the ledger-``removed`` disposition to one config resource row."""
    async with (
        conn.transaction(),
        resource_identity_lock(conn, kind, name),
        conn.cursor(row_factory=dict_row) as cur,
    ):
        await cur.execute(
            "SELECT id FROM resources WHERE id = %s AND managed_by = %s FOR UPDATE",
            (row_id, CONFIG_MANAGED_BY),
        )
        if await cur.fetchone() is None:
            return PruneOutcome(pruned=False, cordoned=False)
        if await _resource_has_any_allocation(cur, row_id):
            await cur.execute(
                "UPDATE resources SET cordoned = true, lease_expires_at = NULL "
                "WHERE id = %s AND NOT cordoned",
                (row_id,),
            )
            return PruneOutcome(pruned=False, cordoned=cur.rowcount == 1)
        await cur.execute("DELETE FROM resources WHERE id = %s", (row_id,))
    return PruneOutcome(pruned=True, cordoned=False)


async def _resource_has_any_allocation(cur: AsyncCursor[dict[str, Any]], row_id: UUID) -> bool:
    """True when any allocation row FK-references the resource."""
    await cur.execute(
        "SELECT 1 FROM allocations WHERE resource_id = %s LIMIT 1",
        (row_id,),
    )
    return await cur.fetchone() is not None


__all__ = [
    "prune_or_cordon_image",
    "prune_or_cordon_removed_resource",
    "prune_or_cordon_resource",
]
