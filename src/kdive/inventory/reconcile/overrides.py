"""The inventory-override ledger GC step (ADR-0199, M2.7 #638).

A ledger entry is **settled** once the file agrees with live state, so reconcile drops it to keep
the ledger bounded. This step runs last in the inventory pass (after the resource sub-pass),
under the same session-scoped ``inventory-reconcile`` lock, with the parsed
:class:`~kdive.inventory.model.InventoryDoc` in hand so it can compare the file's declared
identities and values against the live rows.

Settled rules (per disposition):

* **``removed``** — the identity is **no longer declared** in the file. The operator exported and
  re-applied, so the ordinary file-departure prune now owns the removal and the override is
  redundant.
* **``detached``** — either the live **row no longer exists** (the hand-deleted case: A3 skips the
  re-insert, this step clears the entry, and the next no-entry pass re-asserts the file), **or** the
  file's declared values now **equal** the live row (the override has converged with the file).

A ``removed`` entry whose identity is still declared, or a ``detached`` entry whose file values
still diverge from the live row, is **retained**. The GC never touches a row; it only deletes ledger
entries via :func:`~kdive.inventory.overrides.clear_override`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.domain.catalog.resource_capabilities import (
    CONCURRENT_ALLOCATION_CAP_KEY,
    MEMORY_MB_KEY,
    VCPUS_KEY,
)
from kdive.domain.catalog.resources import ResourceKind
from kdive.inventory.model import InventoryDoc
from kdive.inventory.overrides import (
    InventoryOverride,
    InventoryOverrideDisposition,
    InventorySourceKind,
    OverrideIdentity,
    clear_override,
    lookup_many,
)
from kdive.inventory.reconcile.locks import inventory_pass_lock

# fault-inject shares one synthetic host_uri (mirrors reconcile_resources); the file's host_uri for
# a fault-inject identity is therefore not a divergence signal.
_FAULT_INJECT_HOST_URI = "fault-inject://local"


async def reconcile_overrides_gc(conn: AsyncConnection, doc: InventoryDoc) -> int:
    """Drop every settled inventory-override entry; return the count cleared.

    Runs inside the caller's reconcile pass (already under the ``inventory-reconcile`` lock).

    Args:
        conn: The reconcile pass connection.
        doc: The parsed inventory document the current pass reconciled.

    Returns:
        The number of ledger entries garbage-collected this pass.
    """
    async with inventory_pass_lock(conn), conn.transaction():
        cleared = 0
        cleared += await _gc_resource_overrides(conn, doc)
        return cleared


async def _gc_resource_overrides(conn: AsyncConnection, doc: InventoryDoc) -> int:
    overrides = await lookup_many(conn, InventorySourceKind.RESOURCE)
    if not overrides:
        return 0
    declared = _declared_resource_values(doc)
    live = await _live_resource_values(conn, set(overrides))
    cleared = 0
    for (resource_kind, name), entry in overrides.items():
        if await _resource_entry_is_settled(
            conn, entry, key=(resource_kind, name), declared=declared, live=live
        ):
            cleared += 1
    return cleared


async def _resource_entry_is_settled(
    conn: AsyncConnection,
    entry: InventoryOverride,
    *,
    key: tuple[str, str],
    declared: Mapping[tuple[str, str], dict[str, Any]],
    live: Mapping[tuple[str, str], dict[str, Any]],
) -> bool:
    """Clear ``entry`` if settled; return whether it was cleared."""
    identity = OverrideIdentity(
        source_kind=InventorySourceKind.RESOURCE, resource_kind=key[0], name=key[1]
    )
    if entry.disposition is InventoryOverrideDisposition.REMOVED:
        if key in declared:
            return False  # still declared: the override still suppresses it
        return await clear_override(conn, identity)
    # detached: GC an absent row first (never compare against a missing row), then a converged row.
    live_values = live.get(key)
    if live_values is None:
        return await clear_override(conn, identity)
    declared_values = declared.get(key)
    if declared_values is not None and declared_values == live_values:
        return await clear_override(conn, identity)
    return False


def _declared_resource_values(doc: InventoryDoc) -> dict[tuple[str, str], dict[str, Any]]:
    """The file's declared comparable values per config resource identity ``(kind, name)``."""
    declared: dict[tuple[str, str], dict[str, Any]] = {}
    for inst in doc.fault_inject:
        declared[(ResourceKind.FAULT_INJECT.value, inst.name)] = {
            "cost_class": inst.cost_class,
            "pool": inst.pool,
            "host_uri": _FAULT_INJECT_HOST_URI,
            VCPUS_KEY: inst.vcpus,
            MEMORY_MB_KEY: inst.memory_mb,
            CONCURRENT_ALLOCATION_CAP_KEY: inst.concurrent_allocation_cap,
        }
    for inst in doc.remote_libvirt:
        declared[(ResourceKind.REMOTE_LIBVIRT.value, inst.name)] = {
            "cost_class": inst.cost_class,
            "pool": inst.pool,
            "host_uri": inst.uri,
            VCPUS_KEY: inst.vcpus,
            MEMORY_MB_KEY: inst.memory_mb,
            CONCURRENT_ALLOCATION_CAP_KEY: inst.concurrent_allocation_cap,
        }
    return declared


async def _live_resource_values(
    conn: AsyncConnection, keys: set[tuple[str, str]]
) -> dict[tuple[str, str], dict[str, Any]]:
    """The live comparable values per config resource identity present in ``keys``."""
    live: dict[tuple[str, str], dict[str, Any]] = {}
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT kind, name, cost_class, pool, host_uri, capabilities FROM resources "
            "WHERE managed_by = 'config' AND name IS NOT NULL"
        )
        rows = await cur.fetchall()
    for row in rows:
        key = (str(row["kind"]), str(row["name"]))
        if key not in keys:
            continue
        caps = row["capabilities"]
        caps = caps if isinstance(caps, dict) else {}
        live[key] = {
            "cost_class": row["cost_class"],
            "pool": row["pool"],
            "host_uri": row["host_uri"],
            VCPUS_KEY: caps.get(VCPUS_KEY),
            MEMORY_MB_KEY: caps.get(MEMORY_MB_KEY),
            CONCURRENT_ALLOCATION_CAP_KEY: caps.get(CONCURRENT_ALLOCATION_CAP_KEY),
        }
    return live


__all__ = ["reconcile_overrides_gc"]
