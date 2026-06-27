"""The build-host merge-reconcile (M2.6 #394, ADR-0112).

:func:`reconcile_build_hosts` applies the ``systems.toml`` ``[[build_host]]`` declarations
onto the ``build_hosts`` table (``0027`` / ``0029``). ``managed_by`` governs existence; a
config-declared host is created/updated as ``managed_by='config'`` and carries
``base_image_volume`` / ``workspace_root`` / ``max_concurrent``.

Identity is the build-host **``name``** (already ``text UNIQUE NOT NULL`` in ``0027``), so the
upsert keys on it directly — no migration change. Adopt-on-collision: a config-declared host
whose ``name`` matches an existing ``managed_by='runtime'`` row is **adopted** (flipped to
``config``), never duplicated; the seeded ``worker-local`` baseline from ``0027`` is
``runtime``, so declaring it in config adopts it. (A runtime ``build_hosts.register_ssh`` of a
name that already exists is rejected by the ``name`` UNIQUE constraint, regardless of ownership.)

Kind coverage: the v2 ``[[build_host]]`` model carries no ``address`` / ``ssh_credential_ref``,
so only ``local`` and ``ephemeral_libvirt`` hosts (which need neither) are fully expressible in
config. A config-declared ``ssh`` host cannot satisfy the ``build_hosts_fields_check`` CHECK, so
it is **warned and skipped** rather than aborting the pass — ssh hosts are registered
imperatively via ``build_hosts.register_ssh``, which carries those fields.

Prune is DB-guarded: ``build_host_leases`` FKs ``build_hosts(id) ON DELETE RESTRICT``, so a host
with an in-flight build lease cannot be deleted. Prune therefore **cordons** a busy host
(``enabled = false`` — the disable mechanism the scheduler and reachability probe both honor)
and only **deletes** an idle config host's row, matching the reaper-style refuse-if-live
contract. Prune touches only ``managed_by='config'`` rows. The cordon path SELECTs the
``build_hosts`` row ``FOR UPDATE`` before checking the lease, which conflicts with the implicit
``FOR KEY SHARE`` a concurrent ``build_host_leases`` INSERT takes on the parent row (the FK
check). The two therefore serialize: a lease can never land between the liveness check and the
delete to make the delete hit ``ON DELETE RESTRICT`` and abort the pass.

Declarative ownership note: re-declaring (or adopting) a config host always resets
``enabled = true``, so config is the source of truth for a config-owned host's schedulability.
A consequence is that ``build_hosts.disable`` on a config-owned host is reverted on the next
reconcile pass — to take a config host out of rotation, remove it from ``systems.toml`` (an
idle host's row is then pruned; a busy one is cordoned until its lease drains).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.db.build_hosts import BuildHostKind
from kdive.inventory._row_typing import RowTyper
from kdive.inventory.model import BuildHostInstance, InventoryDoc
from kdive.inventory.overrides import (
    BUILD_HOST_RESOURCE_KIND,
    InventoryOverride,
    InventoryOverrideDisposition,
    InventorySourceKind,
    lookup_many,
)
from kdive.inventory.reconcile import (
    CONFIG_MANAGED_BY,
    ReconcileDiff,
    ReconcileRecord,
    inventory_pass_lock,
    prune_or_cordon_build_host,
)

_log = logging.getLogger(__name__)

# Kinds the v2 [[build_host]] model can fully express (it carries no address/ssh_credential_ref,
# which the build_hosts_fields_check CHECK requires for the 'ssh' kind).
_CONFIG_EXPRESSIBLE_KINDS = (BuildHostKind.LOCAL, BuildHostKind.EPHEMERAL_LIBVIRT)


@dataclass(frozen=True)
class _UpsertBuildHostRow:
    id: UUID
    kind: str
    base_image_volume: str | None
    workspace_root: str
    max_concurrent: int
    enabled: bool
    managed_by: str


@dataclass(frozen=True)
class _PruneBuildHostRow:
    id: UUID
    name: str


async def reconcile_build_hosts(conn: AsyncConnection, doc: InventoryDoc) -> ReconcileDiff:
    """Apply ``doc``'s ``[[build_host]]`` declarations onto ``build_hosts``; prune departed.

    Held under the same session-scoped inventory lock as the image/resource passes, so the
    multi-transaction pass (batched upsert + per-row prunes) never races a second pass into the
    ``name`` UNIQUE constraint.

    Args:
        conn: The reconcile pass connection (a fresh transaction is opened per phase).
        doc: The parsed inventory document.

    Returns:
        The :class:`ReconcileDiff` for the build-host pass.
    """
    diff = ReconcileDiff()
    async with inventory_pass_lock(conn):
        overrides = await lookup_many(conn, InventorySourceKind.BUILD_HOST)
        await _upsert_config_build_hosts(conn, doc, diff, overrides)
        await _prune_departed(conn, doc, diff, overrides)
    return diff


def _disposition(
    overrides: Mapping[tuple[str, str], InventoryOverride], name: str
) -> InventoryOverrideDisposition | None:
    """The override disposition for a build host ``name``, or ``None`` if un-overridden."""
    entry = overrides.get((BUILD_HOST_RESOURCE_KIND, name))
    return entry.disposition if entry is not None else None


async def _upsert_config_build_hosts(
    conn: AsyncConnection,
    doc: InventoryDoc,
    diff: ReconcileDiff,
    overrides: Mapping[tuple[str, str], InventoryOverride],
) -> None:
    """Create or change-detectingly update each config-expressible ``[[build_host]]`` by name."""
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        for inst in doc.build_host:
            reason = _unexpressible_reason(inst)
            if reason is not None:
                diff.warned.append(_record(inst.name, reason))
                _log.warning(
                    "inventory: build host %r not config-expressible: %s", inst.name, reason
                )
                continue
            disposition = _disposition(overrides, inst.name)
            if disposition is InventoryOverrideDisposition.REMOVED:
                continue  # ledger suppresses this identity; the prune sweep removes a live row
            await _upsert_one(cur, inst, diff, detached=disposition is not None)


def _unexpressible_reason(inst: BuildHostInstance) -> str | None:
    """Return why a config build host cannot be realized, or ``None`` if it can.

    The v2 model lacks ``address`` / ``ssh_credential_ref``, so an ``ssh`` host can never
    satisfy the field CHECK; a ``local`` host must carry no ``base_image_volume`` and an
    ``ephemeral_libvirt`` host must carry one (the ``build_hosts_fields_check`` CHECK). Catching
    these here keeps an invalid declaration from aborting the whole pass on a CHECK violation.
    """
    try:
        kind = BuildHostKind(inst.kind)
    except ValueError:
        return (
            f"kind {inst.kind!r} is not config-expressible "
            f"(only {', '.join(kind.value for kind in _CONFIG_EXPRESSIBLE_KINDS)}); "
            "register it imperatively"
        )
    if kind not in _CONFIG_EXPRESSIBLE_KINDS:
        return (
            f"kind {inst.kind!r} is not config-expressible "
            f"(only {', '.join(kind.value for kind in _CONFIG_EXPRESSIBLE_KINDS)}); "
            "register it imperatively"
        )
    if kind is BuildHostKind.EPHEMERAL_LIBVIRT and not (
        inst.base_image_volume and inst.base_image_volume.strip()
    ):
        return "an ephemeral_libvirt build host requires a base_image_volume"
    if kind is BuildHostKind.LOCAL and inst.base_image_volume:
        return "base_image_volume is not valid for a local build host"
    return None


async def _upsert_one(
    cur: Any, inst: BuildHostInstance, diff: ReconcileDiff, *, detached: bool = False
) -> None:
    """Create or update one config build-host row keyed by ``name`` (adopt-on-collision).

    A row whose ``name`` already exists is updated in place and flipped to ``managed_by='config'``
    (adopting a ``runtime`` row), and ``enabled`` is reset ``true`` so re-declaring a previously
    cordoned host re-enables it. Only the config-owned fields are written; ``state`` is left to
    the reachability probe. Append to ``created``/``updated`` only on a real change so a steady
    state is a no-op (the idempotency contract).

    ``detached`` (ADR-0199) marks a build host whose runtime-owned fields the operator owns: a
    **present** row is left untouched (the file does not clobber the runtime ``max_concurrent`` /
    enablement), and an **absent** (hand-deleted) row is **not** re-inserted (that would resurrect
    stale file values under a still-active override) — it is left for the GC step to clear, after
    which the next no-entry pass re-asserts the file.
    """
    if detached:
        return  # present row: runtime owns its values; absent row: GC clears, no-entry re-asserts
    await cur.execute(
        "SELECT id, kind, base_image_volume, workspace_root, max_concurrent, enabled, managed_by "
        "FROM build_hosts WHERE name = %s FOR UPDATE",
        (inst.name,),
    )
    raw_row = await cur.fetchone()
    kind = BuildHostKind(inst.kind)
    base_image_volume = inst.base_image_volume if kind is BuildHostKind.EPHEMERAL_LIBVIRT else None
    if raw_row is None:
        await cur.execute(
            "INSERT INTO build_hosts "
            "(name, kind, base_image_volume, workspace_root, max_concurrent, enabled, managed_by) "
            "VALUES (%s, %s, %s, %s, %s, true, %s)",
            (
                inst.name,
                kind.value,
                base_image_volume,
                inst.workspace_root,
                inst.max_concurrent,
                CONFIG_MANAGED_BY,
            ),
        )
        diff.created.append(_record(inst.name))
        return
    row = _upsert_row(raw_row)
    changed = (
        row.kind != kind.value
        or row.base_image_volume != base_image_volume
        or row.workspace_root != inst.workspace_root
        or row.max_concurrent != inst.max_concurrent
        or row.enabled is not True
        or row.managed_by != CONFIG_MANAGED_BY
    )
    if changed:
        await cur.execute(
            "UPDATE build_hosts SET kind = %s, base_image_volume = %s, workspace_root = %s, "
            "max_concurrent = %s, enabled = true, managed_by = %s WHERE id = %s",
            (
                kind.value,
                base_image_volume,
                inst.workspace_root,
                inst.max_concurrent,
                CONFIG_MANAGED_BY,
                row.id,
            ),
        )
        diff.updated.append(_record(inst.name))


async def _prune_departed(
    conn: AsyncConnection,
    doc: InventoryDoc,
    diff: ReconcileDiff,
    overrides: Mapping[tuple[str, str], InventoryOverride],
) -> None:
    """Prune (or cordon) each config build host whose ``name`` left the file or is ``removed``.

    A ``removed`` ledger entry (ADR-0199) suppresses a still-declared host, so its row is
    cordoned-if-leased / deleted-once-idle even though the file still declares it. A ``detached``
    host's row is runtime-owned and is never pruned here. Unlike resources, a build host carries no
    retained-accounting FK (``build_host_leases`` rows are deleted on release), so the existing
    :func:`prune_or_cordon_build_host` contract (cordon if a lease is in flight, else delete) is
    FK-safe for the ``removed`` path too.
    """
    declared = {inst.name for inst in doc.build_host if _unexpressible_reason(inst) is None}
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, name FROM build_hosts WHERE managed_by = %s", (CONFIG_MANAGED_BY,)
        )
        rows = await cur.fetchall()
    for raw_row in rows:
        row = _prune_row(raw_row)
        name = row.name
        disposition = _disposition(overrides, name)
        is_removed = disposition is InventoryOverrideDisposition.REMOVED
        if name in declared and not is_removed:
            continue
        if disposition is not None and not is_removed:
            continue  # a `detached` row is runtime-owned; never prune it
        outcome = await prune_or_cordon_build_host(conn, row.id)
        record = ReconcileRecord(name=name, entry=f"build_host[{name}]")
        if outcome.cordoned:
            diff.cordoned.append(record)
            _log.info(
                "inventory: config build host %s still has a lease; cordoned (disabled)", name
            )
        elif outcome.pruned:
            diff.pruned.append(record)
            _log.info("inventory: config build host %s absent from config; row pruned", name)


def _record(name: str, detail: str = "") -> ReconcileRecord:
    return ReconcileRecord(name=name, entry=f"build_host[{name}]", detail=detail)


_ROWS = RowTyper("build_hosts")


def _upsert_row(row: Mapping[str, object]) -> _UpsertBuildHostRow:
    return _UpsertBuildHostRow(
        id=_ROWS.uuid(row, "id"),
        kind=_ROWS.string(row, "kind"),
        base_image_volume=_ROWS.optional_string(row, "base_image_volume"),
        workspace_root=_ROWS.string(row, "workspace_root"),
        max_concurrent=_ROWS.integer(row, "max_concurrent"),
        enabled=_ROWS.boolean(row, "enabled"),
        managed_by=_ROWS.string(row, "managed_by"),
    )


def _prune_row(row: Mapping[str, object]) -> _PruneBuildHostRow:
    return _PruneBuildHostRow(id=_ROWS.uuid(row, "id"), name=_ROWS.string(row, "name"))


__all__ = ["reconcile_build_hosts"]
