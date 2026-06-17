"""The resource merge-reconcile (M2.6 #393, ADR-0112) — fixes #385.

:func:`reconcile_resources` applies the ``systems.toml`` provider-instance declarations onto
the ``resources`` table. ``managed_by`` governs **existence**; a **config overlay** applies
declared attributes regardless of who created the row:

* ``cost_class`` → the top-level ``resources.cost_class`` **column** (NOT NULL, read by
  cost-coefficient resolution). It is **never** written into the ``capabilities`` jsonb — that
  would leave the NOT NULL column stale and break pricing.
* ``vcpus`` / ``memory_mb`` / ``concurrent_allocation_cap`` → the ``capabilities`` jsonb. The
  fault-inject ``vcpus`` / ``memory_mb`` here are exactly what #385 lacked: without them a
  kind-targeted ``allocations.request`` is denied ``configuration_error`` before reaching the
  lifecycle.

One creator per kind (avoids a Phase-2 double-create):

* ``local-libvirt`` — **discovery** creates the row (it enumerates real hardware); this
  reconcile **binds** to that row by ``host_uri``, gives it the config instance ``name``, and
  overlays cost/cap **without** touching the discovery-owned ``vcpus`` / ``memory_mb`` / PCIe.
* ``fault-inject`` / ``remote-libvirt`` — this reconcile is the **sole creator**
  (``managed_by='config'``). Their provider discovery is bind-only/non-creating in Phase 2, so
  the legacy env-based discovery and this reconcile never both insert a row for the same host.

Identity is ``(kind, name)`` (the migration's partial-unique index); the ``id`` UUID stays the
PK/FK target. A discovered ``local-libvirt`` host with no config instance keeps its row and is
given a deterministic ``name`` derived from its ``host_uri`` (never pruned — it is
discovery-owned). Prune touches only ``managed_by='config'`` rows; a config resource with a
live allocation is **cordoned**, not deleted (the reaper-style refuse-if-live contract).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypedDict, cast
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kdive.domain.models import ResourceKind
from kdive.domain.resource_capabilities import (
    CONCURRENT_ALLOCATION_CAP_KEY,
    MEMORY_MB_KEY,
    VCPUS_KEY,
)
from kdive.inventory.errors import InventoryError
from kdive.inventory.model import InventoryDoc, LocalLibvirtInstance
from kdive.inventory.reconcile import (
    CONFIG_MANAGED_BY,
    DISCOVERY_MANAGED_BY,
    ReconcileDiff,
    ReconcileRecord,
    inventory_pass_lock,
    prune_or_cordon_resource,
    resource_identity_lock,
)

_log = logging.getLogger(__name__)

# fault-inject has no real host, so every fault-inject instance shares this synthetic host_uri
# and is distinguished by its (kind, name) identity (the Phase-3 multi-instance goal).
_FAULT_INJECT_HOST_URI = "fault-inject://local"
_RESOURCE_UPSERT_SELECT_BY_NAME = (
    "SELECT id, name, host_uri, cost_class, capabilities, managed_by, "
    "lease_expires_at, owner_project, affinity_allowlist "
    "FROM resources WHERE kind = %s AND name = %s FOR UPDATE"
)
_RESOURCE_UPSERT_SELECT_UNNAMED_BY_HOST = (
    "SELECT id, name, host_uri, cost_class, capabilities, managed_by, "
    "lease_expires_at, owner_project, affinity_allowlist "
    "FROM resources WHERE kind = %s AND host_uri = %s AND name IS NULL FOR UPDATE"
)


type ResourceCapValue = str | int | float | bool | None | list[ResourceCapValue] | ResourceCaps
type ResourceCaps = dict[str, ResourceCapValue]


class _CapsRow(TypedDict):
    capabilities: ResourceCaps


class _UpsertResourceRow(_CapsRow):
    id: UUID
    name: str | None
    host_uri: str
    cost_class: str
    managed_by: str
    lease_expires_at: datetime | None
    owner_project: str | None
    affinity_allowlist: list[str]


class _LocalResourceRow(_CapsRow):
    id: UUID
    name: str | None
    cost_class: str


class _PruneResourceRow(TypedDict):
    id: UUID
    kind: str
    name: str


@dataclass(frozen=True)
class _DeclaredResource:
    kind: ResourceKind
    name: str
    host_uri: str
    cost_class: str
    caps: ResourceCaps


async def reconcile_resources(conn: AsyncConnection, doc: InventoryDoc) -> ReconcileDiff:
    """Apply ``doc``'s provider instances onto ``resources`` and prune departed config rows.

    Held under the same session-scoped inventory lock as :func:`reconcile_images`, so the two
    passes never race the ``(kind, name)`` identity constraint.

    Args:
        conn: The reconcile pass connection (a fresh transaction is opened per phase).
        doc: The parsed inventory document.

    Returns:
        The :class:`ReconcileDiff` for the resource pass.
    """
    diff = ReconcileDiff()
    async with inventory_pass_lock(conn):
        await _create_config_resources(conn, doc, diff)
        await _overlay_local_libvirt(conn, doc, diff)
        await _name_unconfigured_discovered(conn, doc, diff)
        await _prune_departed(conn, doc, diff)
    return diff


async def _create_config_resources(
    conn: AsyncConnection, doc: InventoryDoc, diff: ReconcileDiff
) -> None:
    """Upsert the config-owned kinds: fault-inject (no host) and remote-libvirt.

    Both are keyed by their true identity ``(kind, name)`` — the migration's partial-unique
    index. A row whose ``name`` already matches is updated in place (so a changed ``host_uri``
    propagates without a duplicate insert). Only when no name match exists does remote-libvirt
    fall back to **adopting** a discovery row for the same ``host_uri`` whose ``name`` is still
    NULL (the legacy env-based discovery created it), so config and discovery never produce two
    rows for one host.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        for inst in doc.fault_inject:
            caps: ResourceCaps = {
                VCPUS_KEY: inst.vcpus,
                MEMORY_MB_KEY: inst.memory_mb,
                CONCURRENT_ALLOCATION_CAP_KEY: inst.concurrent_allocation_cap,
            }
            declared = _DeclaredResource(
                kind=ResourceKind.FAULT_INJECT,
                name=inst.name,
                host_uri=_FAULT_INJECT_HOST_URI,
                cost_class=inst.cost_class,
                caps=caps,
            )
            async with resource_identity_lock(conn, ResourceKind.FAULT_INJECT, inst.name):
                await _upsert_config_resource(
                    cur,
                    diff,
                    declared=declared,
                    adopt_by_host=False,
                )
        for inst in doc.remote_libvirt:
            caps: ResourceCaps = {
                VCPUS_KEY: inst.vcpus,
                MEMORY_MB_KEY: inst.memory_mb,
                CONCURRENT_ALLOCATION_CAP_KEY: inst.concurrent_allocation_cap,
            }
            declared = _DeclaredResource(
                kind=ResourceKind.REMOTE_LIBVIRT,
                name=inst.name,
                host_uri=inst.uri,
                cost_class=inst.cost_class,
                caps=caps,
            )
            async with resource_identity_lock(conn, ResourceKind.REMOTE_LIBVIRT, inst.name):
                await _upsert_config_resource(
                    cur,
                    diff,
                    declared=declared,
                    adopt_by_host=True,
                    discovery_owned_keys=(VCPUS_KEY, MEMORY_MB_KEY),
                )


async def _upsert_config_resource(
    cur: Any,
    diff: ReconcileDiff,
    *,
    declared: _DeclaredResource,
    adopt_by_host: bool,
    discovery_owned_keys: tuple[str, ...] = (),
) -> None:
    """Create or change-detectingly update one config-owned resource row keyed by (kind, name).

    The lookup is always by the true identity ``(kind, name)`` so a row never collides with
    itself on a ``host_uri`` change (the change is written through). ``adopt_by_host`` additionally
    lets remote-libvirt adopt a legacy-discovery row (same ``host_uri``, ``name IS NULL``) when no
    name match exists, instead of inserting a duplicate. On create supplies the NOT NULL columns
    ``systems.toml`` lacks (``status='available'``, ``pool='default'``). The overlay **merges**
    ``caps`` into the existing capabilities jsonb so a discovery-contributed hardware fact is never
    clobbered. ``cost_class`` lands in the COLUMN, never jsonb. Adoption/update flips ``managed_by``
    to ``config`` and writes the ``name`` + ``host_uri``.

    ``discovery_owned_keys`` are capability keys a discovery row owns as ground truth (e.g.
    ``vcpus`` / ``memory_mb`` hardware facts): a present existing value wins over the
    config-supplied one, so the config value is only a fallback for a pure-config host with no
    discovered size.
    """
    row = await _find_existing(
        cur,
        kind=declared.kind,
        name=declared.name,
        host_uri=declared.host_uri,
        adopt_by_host=adopt_by_host,
    )
    if row is None:
        await _insert_config_resource(
            cur,
            diff,
            declared=declared,
        )
        return
    existing = _caps(row)
    merged = {**existing, **declared.caps}
    for key in discovery_owned_keys:
        if key in existing:
            merged[key] = existing[key]
    if _needs_config_adoption(row):
        await _adopt_config_resource(
            cur,
            diff,
            row=row,
            declared=declared,
            caps=merged,
        )
        return
    await _update_config_resource(
        cur,
        diff,
        row=row,
        declared=declared,
        caps=merged,
    )


async def _insert_config_resource(
    cur: Any,
    diff: ReconcileDiff,
    *,
    declared: _DeclaredResource,
) -> None:
    await cur.execute(
        "INSERT INTO resources (kind, name, capabilities, pool, cost_class, status, "
        " host_uri, managed_by) "
        "VALUES (%s, %s, %s, 'default', %s, 'available', %s, %s)",
        (
            declared.kind.value,
            declared.name,
            Jsonb(declared.caps),
            declared.cost_class,
            declared.host_uri,
            CONFIG_MANAGED_BY,
        ),
    )
    diff.created.append(_record(declared.kind, declared.name))


async def _adopt_config_resource(
    cur: Any,
    diff: ReconcileDiff,
    *,
    row: _UpsertResourceRow,
    declared: _DeclaredResource,
    caps: ResourceCaps,
) -> None:
    """Convert a non-config or scoped resource row into the declared config identity."""
    await cur.execute(
        "UPDATE resources SET name = %s, host_uri = %s, cost_class = %s, capabilities = %s, "
        "managed_by = %s, lease_expires_at = NULL, owner_project = NULL, "
        "affinity_allowlist = '{}' WHERE id = %s",
        (
            declared.name,
            declared.host_uri,
            declared.cost_class,
            Jsonb(caps),
            CONFIG_MANAGED_BY,
            row["id"],
        ),
    )
    diff.updated.append(_record(declared.kind, declared.name))


async def _update_config_resource(
    cur: Any,
    diff: ReconcileDiff,
    *,
    row: _UpsertResourceRow,
    declared: _DeclaredResource,
    caps: ResourceCaps,
) -> None:
    """Apply ordinary config field changes to an already config-managed row."""
    if (
        row["host_uri"] == declared.host_uri
        and row["cost_class"] == declared.cost_class
        and _caps(row) == caps
    ):
        return
    await cur.execute(
        "UPDATE resources SET host_uri = %s, cost_class = %s, capabilities = %s WHERE id = %s",
        (declared.host_uri, declared.cost_class, Jsonb(caps), row["id"]),
    )
    diff.updated.append(_record(declared.kind, declared.name))


def _needs_config_adoption(row: _UpsertResourceRow) -> bool:
    return (
        str(row["managed_by"]) != CONFIG_MANAGED_BY
        or row["name"] is None
        or row["lease_expires_at"] is not None
        or row["owner_project"] is not None
        or list(row["affinity_allowlist"]) != []
    )


async def _find_existing(
    cur: Any, *, kind: ResourceKind, name: str, host_uri: str, adopt_by_host: bool
) -> _UpsertResourceRow | None:
    """Resolve the existing row to upsert: by (kind, name) first, then host-adopt for remote."""
    await cur.execute(
        _RESOURCE_UPSERT_SELECT_BY_NAME,
        (kind.value, name),
    )
    row = await cur.fetchone()
    if row is not None or not adopt_by_host:
        return _upsert_row(row) if row is not None else None
    await cur.execute(
        _RESOURCE_UPSERT_SELECT_UNNAMED_BY_HOST,
        (kind.value, host_uri),
    )
    row = await cur.fetchone()
    return _upsert_row(row) if row is not None else None


async def _overlay_local_libvirt(
    conn: AsyncConnection, doc: InventoryDoc, diff: ReconcileDiff
) -> None:
    """Overlay cost/cap onto discovery-created local-libvirt rows; never create or overwrite HW.

    Binds by ``host_uri`` to the discovery row, gives it the config ``name``, sets the
    ``cost_class`` column, and merges only ``concurrent_allocation_cap`` into the capabilities
    jsonb — the discovery-owned ``vcpus`` / ``memory_mb`` / PCIe keys are left untouched.
    """
    if not doc.local_libvirt:
        return
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        for inst in doc.local_libvirt:
            await _overlay_one_local(cur, inst, diff)


async def _overlay_one_local(cur: Any, inst: LocalLibvirtInstance, diff: ReconcileDiff) -> None:
    await cur.execute(
        "SELECT id, name, cost_class, capabilities FROM resources "
        "WHERE kind = %s AND host_uri = %s FOR UPDATE",
        (ResourceKind.LOCAL_LIBVIRT.value, inst.host_uri),
    )
    row = await cur.fetchone()
    if row is None:
        diff.warned.append(
            _record(
                ResourceKind.LOCAL_LIBVIRT,
                inst.name,
                f"no discovered local-libvirt host at {inst.host_uri}; overlay deferred",
            )
        )
        return
    row = _local_row(row)
    merged = {**_caps(row), CONCURRENT_ALLOCATION_CAP_KEY: inst.concurrent_allocation_cap}
    changed = (
        row["name"] != inst.name or row["cost_class"] != inst.cost_class or _caps(row) != merged
    )
    if changed:
        await cur.execute(
            "UPDATE resources SET name = %s, cost_class = %s, capabilities = %s WHERE id = %s",
            (inst.name, inst.cost_class, Jsonb(merged), row["id"]),
        )
        diff.updated.append(_record(ResourceKind.LOCAL_LIBVIRT, inst.name))


async def _name_unconfigured_discovered(
    conn: AsyncConnection, doc: InventoryDoc, diff: ReconcileDiff
) -> None:
    """Give every discovery row without a config instance a deterministic name from host_uri."""
    configured = {inst.host_uri for inst in doc.local_libvirt}
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, host_uri FROM resources WHERE managed_by = %s AND name IS NULL FOR UPDATE",
            (DISCOVERY_MANAGED_BY,),
        )
        rows = await cur.fetchall()
        for row in rows:
            if row["host_uri"] in configured:
                continue  # the local-libvirt overlay names this one
            name = _deterministic_name(str(row["host_uri"]))
            await cur.execute("UPDATE resources SET name = %s WHERE id = %s", (name, row["id"]))


async def _prune_departed(conn: AsyncConnection, doc: InventoryDoc, diff: ReconcileDiff) -> None:
    """Prune (or cordon) each config resource whose (kind, name) left the file."""
    declared = _declared_config_identities(doc)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, kind, name FROM resources WHERE managed_by = %s", (CONFIG_MANAGED_BY,)
        )
        rows = await cur.fetchall()
    for raw in rows:
        row = _prune_row(raw)
        identity = (str(row["kind"]), str(row["name"]))
        if identity in declared:
            continue
        name = str(row["name"])
        kind = ResourceKind(str(row["kind"]))
        outcome = await prune_or_cordon_resource(conn, _row_id(row), name, kind=kind)
        record = ReconcileRecord(name=name, entry=f"resource[{identity[0]}/{name}]")
        if outcome.cordoned:
            diff.cordoned.append(record)
            _log.info("inventory: config resource %s still in use; cordoned (not pruned)", name)
        elif outcome.pruned:
            diff.pruned.append(record)
            _log.info("inventory: config resource %s absent from config; row pruned", name)


def _declared_config_identities(doc: InventoryDoc) -> set[tuple[str, str]]:
    """The (kind, name) identities the file declares for the config-owned (sole-creator) kinds.

    Only ``fault_inject`` / ``remote_libvirt`` are config-owned; ``local_libvirt`` rows are
    discovery-owned and are never pruned by this reconcile.
    """
    identities: set[tuple[str, str]] = set()
    for inst in doc.fault_inject:
        identities.add((ResourceKind.FAULT_INJECT.value, inst.name))
    for inst in doc.remote_libvirt:
        identities.add((ResourceKind.REMOTE_LIBVIRT.value, inst.name))
    return identities


def _deterministic_name(host_uri: str) -> str:
    """A stable, readable name for an unconfigured discovered host derived from its host_uri."""
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in host_uri).strip("-")
    return f"discovered-{cleaned}" if cleaned else "discovered-host"


def _caps(row: _CapsRow) -> ResourceCaps:
    return row["capabilities"]


def _resource_caps(value: object) -> ResourceCaps:
    if not isinstance(value, dict):
        raise InventoryError("resources", "capabilities", "database row is not a JSON object")
    return cast("ResourceCaps", value)


def _upsert_row(row: dict[str, Any]) -> _UpsertResourceRow:
    row_id = _expect_uuid(row, "id")
    name = _expect_optional_str(row, "name")
    host_uri = _expect_str(row, "host_uri")
    cost_class = _expect_str(row, "cost_class")
    managed_by = _expect_str(row, "managed_by")
    lease_expires_at = _expect_optional_datetime(row, "lease_expires_at")
    owner_project = _expect_optional_str(row, "owner_project")
    affinity_allowlist = _expect_str_list(row, "affinity_allowlist")
    return {
        "id": row_id,
        "name": name,
        "host_uri": host_uri,
        "cost_class": cost_class,
        "managed_by": managed_by,
        "lease_expires_at": lease_expires_at,
        "owner_project": owner_project,
        "affinity_allowlist": affinity_allowlist,
        "capabilities": _resource_caps(row["capabilities"]),
    }


def _local_row(row: dict[str, Any]) -> _LocalResourceRow:
    return {
        "id": _expect_uuid(row, "id"),
        "name": _expect_optional_str(row, "name"),
        "cost_class": _expect_str(row, "cost_class"),
        "capabilities": _resource_caps(row["capabilities"]),
    }


def _prune_row(row: dict[str, Any]) -> _PruneResourceRow:
    return {
        "id": _expect_uuid(row, "id"),
        "kind": _expect_str(row, "kind"),
        "name": _expect_str(row, "name"),
    }


def _expect_uuid(row: dict[str, Any], field: str) -> UUID:
    value = row[field]
    if not isinstance(value, UUID):
        raise _row_error(field, "uuid")
    return value


def _expect_str(row: dict[str, Any], field: str) -> str:
    value = row[field]
    if not isinstance(value, str):
        raise _row_error(field, "str")
    return value


def _expect_optional_str(row: dict[str, Any], field: str) -> str | None:
    value = row[field]
    if value is not None and not isinstance(value, str):
        raise _row_error(field, "str or null")
    return value


def _expect_optional_datetime(row: dict[str, Any], field: str) -> datetime | None:
    value = row[field]
    if value is not None and not isinstance(value, datetime):
        raise _row_error(field, "datetime or null")
    return value


def _expect_str_list(row: dict[str, Any], field: str) -> list[str]:
    value = row[field]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _row_error(field, "list[str]")
    return value


def _row_error(field: str, expected: str) -> InventoryError:
    return InventoryError("resources", field, f"database row expected {expected}")


def _record(kind: ResourceKind, name: str, detail: str = "") -> ReconcileRecord:
    return ReconcileRecord(name=name, entry=f"resource[{kind.value}/{name}]", detail=detail)


def _row_id(row: _PruneResourceRow) -> UUID:
    return row["id"]


__all__ = ["reconcile_resources"]
