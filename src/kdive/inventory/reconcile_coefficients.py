"""The cost-class coefficient merge-reconcile (ADR-0115 §2/§3).

Upserts each ``[[cost_class]]`` declaration into ``cost_class_coefficients``
**file-authoritatively**: a declared class is re-asserted to the file value on every pass,
including the continuous reconciler loop. It is **upsert-only** — a class removed from the
file simply stops being re-asserted (its last value persists); reconcile never deletes a
coefficient, so an in-flight host can never be mispriced by a reconcile-driven delete.

Drift detection is **atomic with the write**: each row is taken under ``SELECT coeff …
FOR UPDATE`` in its own transaction, then written, so a concurrent
``ops.set_cost_class_coeff`` cannot slip between a separate read and the clobber and be
reverted unlogged. (Plain ``INSERT … ON CONFLICT DO UPDATE … RETURNING`` returns the
*post*-update row, not the prior ``coeff``, so it cannot supply the "was Y" — the locked
read is required.) When the prior value differs from the file value, the pass records a
``warned`` entry (the one behavior that *changes* a value is never silent) **and** logs the
drift line; the on-demand ``ops.reconcile_systems`` path folds ``warned`` into its
``platform_audit_log`` row. An idempotent re-run (file == DB) produces no diff and no log
noise.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from psycopg import AsyncConnection

from kdive.inventory.model import CostClassEntry, InventoryDoc
from kdive.inventory.reconcile import ReconcileDiff, ReconcileRecord, inventory_pass_lock

_log = logging.getLogger(__name__)


async def reconcile_coefficients(conn: AsyncConnection, doc: InventoryDoc) -> ReconcileDiff:
    """Upsert ``doc``'s ``[[cost_class]]`` declarations file-authoritatively; flag drift.

    Held under the same session-scoped inventory lock as the sibling passes, so a
    multi-row pass never races a second pass. Each declared class is handled in its own
    transaction (a brief ``FOR UPDATE`` hold), so drift is detected atomically with the
    write.

    Args:
        conn: The reconcile pass connection (a fresh transaction is opened per row).
        doc: The parsed inventory document.

    Returns:
        The :class:`ReconcileDiff` for the coefficient pass (``created`` for new rows,
        ``updated`` + ``warned`` for a value the file overrides; empty on a no-op).
    """
    diff = ReconcileDiff()
    async with inventory_pass_lock(conn):
        for entry in doc.cost_class:
            await _upsert_one(conn, entry, diff)
    return diff


async def _upsert_one(conn: AsyncConnection, entry: CostClassEntry, diff: ReconcileDiff) -> None:
    """Create or change-detectingly re-assert one coefficient under a per-row lock.

    File-authoritative even under a create race. ``SELECT … FOR UPDATE`` locks nothing when
    the row is absent, so a concurrent ``ops.set_cost_class_coeff`` of the same brand-new
    class can land between the read and our insert. ``INSERT … ON CONFLICT DO NOTHING
    RETURNING coeff`` distinguishes the two outcomes: if our row was inserted we record
    ``created``; if a concurrent insert won the PK (no row returned) we **re-read under the
    lock and fall through to the same compare-and-apply path the existing-row case uses**, so
    the file value still wins this pass and the diff reflects reality (no false ``created``,
    no stale concurrent value left for a whole reconcile interval).
    """
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            "SELECT coeff FROM cost_class_coefficients WHERE cost_class = %s FOR UPDATE",
            (entry.name,),
        )
        row = await cur.fetchone()
        if row is None:
            await cur.execute(
                "INSERT INTO cost_class_coefficients (cost_class, coeff) VALUES (%s, %s) "
                "ON CONFLICT (cost_class) DO NOTHING RETURNING coeff",
                (entry.name, entry.coeff),
            )
            if await cur.fetchone() is not None:
                diff.created.append(_record(entry.name, f"priced at {entry.coeff}"))
                return
            # A concurrent insert won the PK; re-read under the lock and apply the file value
            # through the compare path below (there is no delete path, so the row now exists).
            await cur.execute(
                "SELECT coeff FROM cost_class_coefficients WHERE cost_class = %s FOR UPDATE",
                (entry.name,),
            )
            row = await cur.fetchone()
            assert row is not None, (  # no coefficient-delete path exists; the row persists
                f"cost_class {entry.name!r} vanished after a conflicting insert"
            )
        prior = Decimal(row[0])
        if prior == entry.coeff:
            return  # idempotent: no write, no diff, no log noise
        await cur.execute(
            "UPDATE cost_class_coefficients SET coeff = %s WHERE cost_class = %s",
            (entry.coeff, entry.name),
        )
        detail = f"re-asserted from file: was {prior}, now {entry.coeff}"
        diff.updated.append(_record(entry.name, detail))
        diff.warned.append(_record(entry.name, detail))
        _log.warning("inventory: cost_class %r %s", entry.name, detail)


def _record(name: str, detail: str) -> ReconcileRecord:
    return ReconcileRecord(name=name, entry=f"cost_class[{name}]", detail=detail)
