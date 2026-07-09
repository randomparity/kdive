"""The inventory-override ledger repository (ADR-0199, M2.7 #638).

The ledger records, per inventory identity, an operator's intent to **override**
``systems.toml``. It is the provenance record that lets a runtime mutation of a config-declared
identity win over the file without losing drift repair for identities that carry no entry: the
reconcile inventory pass (``reconcile_resources``) consults it under
the session-scoped ``inventory-reconcile`` lock, and a GC step (``reconciler/inventory.py``) drops
a settled entry once the file agrees with live state.

The two dispositions:

* ``detached`` — "runtime owns the live row; ignore the file's *values* for it." The row keeps
  ``managed_by='config'`` (so a future export still emits it) but reconcile stops overwriting its
  runtime-owned fields from the file.
* ``removed`` — "suppress this identity; do not re-create it." Reconcile skips re-creating the
  identity and deletes a cordoned row once it is idle.

Every helper takes an injected :class:`~psycopg.AsyncConnection` and does **not** open its own
transaction or take a lock. The caller owns the transaction and the per-identity
``resource_identity_lock`` so the ledger write and the row change commit atomically (ADR-0199
concurrency). The reconcile pass reads under its own pass lock.

Identity is ``(source_kind, resource_kind, name)`` — the ledger table's PK. ``source_kind`` is the
inventory family (``resource`` | ``build_host``); ``resource_kind`` is the resource ``kind`` for a
resource, or the fixed sentinel ``build-host`` for a build host (build-host names are globally
unique, so the sentinel keeps the PK total).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from psycopg import AsyncConnection
from psycopg.rows import dict_row

__all__ = [
    "InventoryOverrideDisposition",
    "InventorySourceKind",
    "BUILD_HOST_RESOURCE_KIND",
    "OverrideIdentity",
    "InventoryOverride",
    "set_override",
    "clear_override",
    "lookup",
    "lookup_many",
]


class InventoryOverrideDisposition(StrEnum):
    """The override dispositions; mirrored by ``inventory_overrides_disposition_check`` (0046)."""

    DETACHED = "detached"
    REMOVED = "removed"


class InventorySourceKind(StrEnum):
    """The inventory family an override targets (the ledger's ``source_kind``)."""

    RESOURCE = "resource"
    BUILD_HOST = "build_host"


# Build-host names are globally unique, so a build-host override fixes resource_kind to this
# sentinel — the PK stays (source_kind, resource_kind, name) for both families.
BUILD_HOST_RESOURCE_KIND = "build-host"


@dataclass(frozen=True)
class OverrideIdentity:
    """The ledger PK for one inventory identity ``(source_kind, resource_kind, name)``."""

    source_kind: InventorySourceKind
    resource_kind: str
    name: str

    @property
    def key(self) -> tuple[str, str]:
        """The ``(resource_kind, name)`` sub-key a per-``source_kind`` bulk lookup is keyed on."""
        return (self.resource_kind, self.name)


@dataclass(frozen=True)
class InventoryOverride:
    """One ledger row as read back from the table."""

    source_kind: InventorySourceKind
    resource_kind: str
    name: str
    disposition: InventoryOverrideDisposition
    reason: str
    actor: str
    created_at: datetime


async def set_override(
    conn: AsyncConnection,
    identity: OverrideIdentity,
    *,
    disposition: InventoryOverrideDisposition,
    reason: str,
    actor: str,
) -> None:
    """Write (or replace) the override for ``identity``.

    Upserts on the PK so re-setting an override for the same identity replaces the
    disposition/reason/actor in place (a remove-then-re-remove is idempotent). ``created_at`` is
    left at its existing value on conflict, so it records the first time the override was set.

    Args:
        conn: A connection inside the caller's open transaction (which also holds the per-identity
            lock).
        identity: The inventory identity to override.
        disposition: ``detached`` or ``removed``.
        reason: The operator-supplied audit reason.
        actor: The principal that set the override.
    """
    await conn.execute(
        "INSERT INTO inventory_overrides "
        "(source_kind, resource_kind, name, disposition, reason, actor) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (source_kind, resource_kind, name) DO UPDATE SET "
        "disposition = EXCLUDED.disposition, reason = EXCLUDED.reason, actor = EXCLUDED.actor",
        (
            identity.source_kind.value,
            identity.resource_kind,
            identity.name,
            disposition.value,
            reason,
            actor,
        ),
    )


async def clear_override(conn: AsyncConnection, identity: OverrideIdentity) -> bool:
    """Delete the override for ``identity``; return whether a row was removed.

    Args:
        conn: A connection inside the caller's open transaction.
        identity: The inventory identity whose override to clear.

    Returns:
        ``True`` when a row was deleted, ``False`` when no override existed.
    """
    cur = await conn.execute(
        "DELETE FROM inventory_overrides "
        "WHERE source_kind = %s AND resource_kind = %s AND name = %s",
        (identity.source_kind.value, identity.resource_kind, identity.name),
    )
    return cur.rowcount > 0


async def lookup(conn: AsyncConnection, identity: OverrideIdentity) -> InventoryOverride | None:
    """Fetch the override for ``identity``, or ``None`` when none is set."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT source_kind, resource_kind, name, disposition, reason, actor, created_at "
            "FROM inventory_overrides "
            "WHERE source_kind = %s AND resource_kind = %s AND name = %s",
            (identity.source_kind.value, identity.resource_kind, identity.name),
        )
        row = await cur.fetchone()
    return _override(row) if row is not None else None


async def lookup_many(
    conn: AsyncConnection, source_kind: InventorySourceKind
) -> dict[tuple[str, str], InventoryOverride]:
    """Bulk-fetch every override for one inventory family, keyed by ``(resource_kind, name)``.

    One query per reconcile pass (not one per identity), so the pass reads the whole ledger family
    under its lock and looks each declared/departed identity up in the returned dict.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT source_kind, resource_kind, name, disposition, reason, actor, created_at "
            "FROM inventory_overrides WHERE source_kind = %s",
            (source_kind.value,),
        )
        rows = await cur.fetchall()
    result: dict[tuple[str, str], InventoryOverride] = {}
    for row in rows:
        override = _override(row)
        result[(override.resource_kind, override.name)] = override
    return result


def _override(row: dict[str, object]) -> InventoryOverride:
    return InventoryOverride(
        source_kind=InventorySourceKind(_str(row, "source_kind")),
        resource_kind=_str(row, "resource_kind"),
        name=_str(row, "name"),
        disposition=InventoryOverrideDisposition(_str(row, "disposition")),
        reason=_str(row, "reason"),
        actor=_str(row, "actor"),
        created_at=_dt(row, "created_at"),
    )


def _str(row: dict[str, object], key: str) -> str:
    value = row[key]
    if not isinstance(value, str):
        raise TypeError(f"inventory_overrides.{key} is not text: {value!r}")
    return value


def _dt(row: dict[str, object], key: str) -> datetime:
    value = row[key]
    if not isinstance(value, datetime):
        raise TypeError(f"inventory_overrides.{key} is not a timestamp: {value!r}")
    return value
