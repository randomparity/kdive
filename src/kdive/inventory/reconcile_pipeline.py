"""The one ordered resource-reconciling chain (ADR-0115 §2).

Two orchestrators reconcile resources from ``systems.toml`` — the background reconciler
loop (``reconciler/inventory.py``) and the on-demand ``ops.reconcile_systems`` MCP tool.
Both call :func:`reconcile_all` so the ordering is defined once: the **coefficient pass runs
before the resource pass**, so a config host that declares both a ``cost_class`` and a
matching ``[[cost_class]]`` block is priced in the same pass that creates its row — closing
the unpriced-cost_class admission wall. The images-only CLI (``inventory/reconcile_cli.py``)
reconciles no resources and does not use this helper.
"""

from __future__ import annotations

from psycopg import AsyncConnection

from kdive.inventory.model import InventoryDoc
from kdive.inventory.reconcile import ReconcileDiff
from kdive.inventory.reconcile_build_configs import reconcile_build_configs
from kdive.inventory.reconcile_build_hosts import reconcile_build_hosts
from kdive.inventory.reconcile_coefficients import reconcile_coefficients
from kdive.inventory.reconcile_images import ImageHeadStore, reconcile_images
from kdive.inventory.reconcile_resources import reconcile_resources


async def reconcile_all(
    conn: AsyncConnection, doc: InventoryDoc, store: ImageHeadStore
) -> ReconcileDiff:
    """Reconcile ``doc`` into the catalog in dependency order; return one merged diff.

    Order: images → **coefficients → resources** → build hosts → build configs. Coefficients
    precede resources so a host lands priced (ADR-0115 §2); build configs run last, with no
    cross-entity dependency (ADR-0122 §4). Each sub-pass owns its own locks and transactions;
    this helper only sequences them and folds the per-entity diffs.
    """
    merged = ReconcileDiff()
    _extend(merged, await reconcile_images(conn, doc, store))
    _extend(merged, await reconcile_coefficients(conn, doc))
    _extend(merged, await reconcile_resources(conn, doc))
    _extend(merged, await reconcile_build_hosts(conn, doc))
    _extend(merged, await reconcile_build_configs(conn, doc, store))
    return merged


def _extend(into: ReconcileDiff, part: ReconcileDiff) -> None:
    into.created.extend(part.created)
    into.updated.extend(part.updated)
    into.pruned.extend(part.pruned)
    into.cordoned.extend(part.cordoned)
    into.warned.extend(part.warned)
