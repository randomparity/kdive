"""Typed async CRUD over the durable objects (ADR-0003, ADR-0016).

A base `Repository[M]` provides `insert` / `get`; `StatefulRepository[M, S]` adds
`update_state`, guarded by `kdive.domain.capacity.state.can_transition` and bound to the
object's state enum `S`. Module-level instances bind these to each table. Rows map to
Pydantic models field-for-column; the database owns the `created_at` / `updated_at`
timestamps (they are omitted from inserts and read back via `RETURNING *`).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from kdive.domain._records import DomainModel
from kdive.domain.accounting.records import Budget, CostClassCoefficient, LedgerEntry, Quota
from kdive.domain.capacity.state import (
    AllocationState,
    DebugSessionState,
    InvestigationState,
    JobState,
    ResourceStatus,
    RunState,
    SnapshotState,
    SystemState,
    ensure_transition,
)
from kdive.domain.catalog.artifacts import Artifact
from kdive.domain.catalog.images import ImageCatalogEntry
from kdive.domain.catalog.resources import Resource
from kdive.domain.lifecycle.records import (
    Allocation,
    DebugSession,
    Investigation,
    Run,
    Snapshot,
    System,
    SystemShape,
)
from kdive.domain.operations.jobs import Job
from kdive.serialization import JsonValue

# DB-authoritative columns, omitted from inserts so their defaults/trigger apply.
_SERVER_GENERATED = ("created_at", "updated_at")


class ObjectNotFound(RuntimeError):
    """An `update_state` target id does not exist — a consistency error."""


class Repository[M: BaseModel]:
    """Async `insert` / `get` for one table.

    Columns in ``server_generated`` are omitted from inserts so the DB default/trigger
    fills them; ``key_column`` is the lookup column ``get`` filters on (``id`` for the
    durable objects, the natural key for the accounting tables).
    """

    def __init__(
        self,
        model: type[M],
        table: str,
        *,
        json_columns: frozenset[str] = frozenset(),
        server_generated: tuple[str, ...] = _SERVER_GENERATED,
        key_column: str = "id",
    ) -> None:
        self._model = model
        self._table = table
        self._json_columns = json_columns
        self._key_column = key_column
        self._insert_columns = tuple(
            name for name in model.model_fields if name not in server_generated
        )

    def _insert_params(self, obj: M) -> dict[str, Any]:
        dumped = obj.model_dump()
        return {
            name: Jsonb(dumped[name])
            if name in self._json_columns and dumped[name] is not None
            else _to_db_value(dumped[name])
            for name in self._insert_columns
        }

    async def insert(self, conn: AsyncConnection, obj: M) -> M:
        """Insert ``obj`` and return it as persisted (DB-authoritative timestamps)."""
        query = sql.SQL("INSERT INTO {table} ({cols}) VALUES ({vals}) RETURNING *").format(
            table=sql.Identifier(self._table),
            cols=sql.SQL(", ").join(sql.Identifier(c) for c in self._insert_columns),
            vals=sql.SQL(", ").join(sql.Placeholder(c) for c in self._insert_columns),
        )
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, self._insert_params(obj))
            row = await cur.fetchone()
        if row is None:  # Invariant: INSERT ... RETURNING always yields one row.
            raise RuntimeError(f"INSERT into {self._table} returned no row")
        return self._model.model_validate(row)

    async def get(self, conn: AsyncConnection, key: UUID | str) -> M | None:
        """Return the row whose ``key_column`` equals ``key``, or ``None`` if absent."""
        query = sql.SQL("SELECT * FROM {table} WHERE {col} = %s").format(
            table=sql.Identifier(self._table), col=sql.Identifier(self._key_column)
        )
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, (key,))
            row = await cur.fetchone()
        return None if row is None else self._model.model_validate(row)

    async def list_all(self, conn: AsyncConnection) -> list[M]:
        """Return every row, ordered by ``key_column`` for a stable collection envelope."""
        query = sql.SQL("SELECT * FROM {table} ORDER BY {col}").format(
            table=sql.Identifier(self._table), col=sql.Identifier(self._key_column)
        )
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query)
            rows = await cur.fetchall()
        return [self._model.model_validate(row) for row in rows]

    async def delete(self, conn: AsyncConnection, key: UUID | str) -> bool:
        """Delete the row whose ``key_column`` equals ``key``; return whether one was removed.

        Assumes ``key_column`` is unique (a PK or unique constraint) — the ``== 1`` check
        reports ``True`` for the single matched row. On a non-unique column a multi-row delete
        would report ``False`` despite removing rows, so only use this on a uniquely-keyed table.
        """
        query = sql.SQL("DELETE FROM {table} WHERE {col} = %s").format(
            table=sql.Identifier(self._table), col=sql.Identifier(self._key_column)
        )
        async with conn.cursor() as cur:
            await cur.execute(query, (key,))
            return cur.rowcount == 1


class StatefulRepository[M: DomainModel, S: StrEnum](Repository[M]):
    """A `Repository` plus `update_state`, bound to the object's state enum ``S``."""

    def __init__(
        self,
        model: type[M],
        table: str,
        state_enum: type[S],
        *,
        state_column: str = "state",
        json_columns: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(model, table, json_columns=json_columns)
        self._state_enum = state_enum
        self._state_column = state_column

    async def update_state(self, conn: AsyncConnection, obj_id: UUID, new_state: S) -> M:
        """Transition ``obj_id`` to ``new_state`` if `can_transition` permits it.

        Reads the current state under `FOR UPDATE` and writes in one transaction, so
        concurrent updaters are serialized.

        Raises:
            ObjectNotFound: No row has ``obj_id``.
            IllegalTransition: The current → ``new_state`` edge is not permitted.
        """
        col = self._state_column
        table = sql.Identifier(self._table)
        col_id = sql.Identifier(col)
        select_q = sql.SQL("SELECT {col} FROM {table} WHERE id = %s FOR UPDATE").format(
            col=col_id, table=table
        )
        update_q = sql.SQL("UPDATE {table} SET {col} = %s WHERE id = %s RETURNING *").format(
            table=table, col=col_id
        )
        async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(select_q, (obj_id,))
            row = await cur.fetchone()
            if row is None:
                raise ObjectNotFound(f"{self._table} id {obj_id} does not exist")
            ensure_transition(self._state_enum(row[col]), new_state)
            await cur.execute(update_q, (new_state, obj_id))
            updated = await cur.fetchone()
        if updated is None:  # Invariant: the row was held under FOR UPDATE.
            raise RuntimeError(f"UPDATE of {self._table} id {obj_id} returned no row")
        return self._model.model_validate(updated)

    async def set_json_column(
        self,
        conn: AsyncConnection,
        obj_id: UUID,
        column: str,
        value: dict[str, JsonValue] | None,
        allowed_states: frozenset[S],
    ) -> bool:
        """Write a jsonb ``column`` only while the row is in ``allowed_states`` (ADR-0369).

        A state-guarded payload write, distinct from :meth:`update_state` (which transitions the
        state column). The ``WHERE state = ANY(...)`` guard makes the write a no-op on a row that
        has left ``allowed_states`` — e.g. a System that crashed / was reaped between a
        post-provision live read and this write — so a stale value can never land on a terminal row.

        Args:
            column: The jsonb column to write. Serialized with ``Jsonb`` (``None`` writes SQL NULL).
            allowed_states: The row states in which the write is permitted.

        Returns:
            ``True`` if a row was updated, ``False`` if the guard matched no row (a no-op).
        """
        payload = Jsonb(value) if value is not None else None
        query = sql.SQL(
            "UPDATE {table} SET {column} = %s WHERE id = %s AND {state} = ANY(%s) RETURNING id"
        ).format(
            table=sql.Identifier(self._table),
            column=sql.Identifier(column),
            state=sql.Identifier(self._state_column),
        )
        async with conn.cursor() as cur:
            await cur.execute(query, (payload, obj_id, [s.value for s in allowed_states]))
            return await cur.fetchone() is not None


class KeyedRepository[M: BaseModel](Repository[M]):
    """A `Repository` for a natural-key table that supports `upsert`.

    The accounting tables (`budgets`, `quotas`, `cost_class_coefficients`) are keyed
    by `project` / `cost_class`, not a generated `id`, and admin re-sets overwrite an
    existing row. `upsert` writes the row or, on a primary-key conflict, updates only
    ``update_columns`` — letting `budgets` re-set `limit_kcu` without clobbering the
    DB-maintained `spent_kcu` running total.
    """

    def __init__(
        self,
        model: type[M],
        table: str,
        key_column: str,
        *,
        update_columns: frozenset[str] | None = None,
        json_columns: frozenset[str] = frozenset(),
    ) -> None:
        if key_column not in model.model_fields:
            raise ValueError(f"{model.__name__} has no field {key_column!r} to key on")
        super().__init__(
            model,
            table,
            json_columns=json_columns,
            server_generated=("updated_at",),
            key_column=key_column,
        )
        candidates = update_columns or (frozenset(self._insert_columns) - {key_column})
        self._update_columns = tuple(c for c in self._insert_columns if c in candidates)

    async def upsert(self, conn: AsyncConnection, obj: M) -> M:
        """Insert ``obj``; on a primary-key conflict update only ``update_columns``."""
        assignments = sql.SQL(", ").join(
            sql.SQL("{col} = EXCLUDED.{col}").format(col=sql.Identifier(c))
            for c in self._update_columns
        )
        query = sql.SQL(
            "INSERT INTO {table} ({cols}) VALUES ({vals}) "
            "ON CONFLICT ({key}) DO UPDATE SET {assignments} RETURNING *"
        ).format(
            table=sql.Identifier(self._table),
            cols=sql.SQL(", ").join(sql.Identifier(c) for c in self._insert_columns),
            vals=sql.SQL(", ").join(sql.Placeholder(c) for c in self._insert_columns),
            key=sql.Identifier(self._key_column),
            assignments=assignments,
        )
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, self._insert_params(obj))
            row = await cur.fetchone()
        if row is None:  # Invariant: INSERT ... RETURNING always yields one row.
            raise RuntimeError(f"UPSERT into {self._table} returned no row")
        return self._model.model_validate(row)


def _to_db_value(value: object) -> object:
    """Convert enum-backed domain scalars to their SQL column representation."""
    if isinstance(value, StrEnum):
        return value.value
    return value


RESOURCES = StatefulRepository(
    Resource,
    "resources",
    ResourceStatus,
    state_column="status",
    json_columns=frozenset({"capabilities"}),
)
ALLOCATIONS = StatefulRepository(
    Allocation,
    "allocations",
    AllocationState,
    json_columns=frozenset({"pcie_claim", "requested_pcie_specs"}),
)
SYSTEMS = StatefulRepository(
    System,
    "systems",
    SystemState,
    json_columns=frozenset({"provisioning_profile", "resolved_cpu"}),
)
INVESTIGATIONS = StatefulRepository(
    Investigation, "investigations", InvestigationState, json_columns=frozenset({"external_refs"})
)
RUNS = StatefulRepository(
    Run,
    "runs",
    RunState,
    json_columns=frozenset({"build_profile", "expected_boot_failure"}),
)
DEBUG_SESSIONS = StatefulRepository(DebugSession, "debug_sessions", DebugSessionState)
SNAPSHOTS = StatefulRepository(Snapshot, "snapshots", SnapshotState)


async def snapshot_by_name(conn: AsyncConnection, system_id: UUID, name: str) -> Snapshot | None:
    """Return the ``(system_id, name)`` snapshot row, or ``None`` (the UNIQUE lookup)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM snapshots WHERE system_id = %s AND name = %s", (system_id, name)
        )
        row = await cur.fetchone()
    return None if row is None else Snapshot.model_validate(row)


async def snapshots_for_system(conn: AsyncConnection, system_id: UUID) -> list[Snapshot]:
    """Return a System's snapshots newest-first (the ``systems.list_snapshots`` read)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM snapshots WHERE system_id = %s ORDER BY created_at DESC, id DESC",
            (system_id,),
        )
        rows = await cur.fetchall()
    return [Snapshot.model_validate(row) for row in rows]


async def delete_snapshots_for_system(conn: AsyncConnection, system_id: UUID) -> None:
    """Delete every snapshot ledger row for a System (teardown/reprovision reclaim, ADR-0378).

    The libvirt snapshot data is freed with the overlay qcow2 at teardown and destroyed by the
    recreated disk at reprovision, so the ledger rows are removed to match. A no-op when none
    exist; the ``ON DELETE CASCADE`` FK still covers the eventual System-row delete at release.
    """
    await conn.execute("DELETE FROM snapshots WHERE system_id = %s", (system_id,))


JOBS = StatefulRepository(
    Job,
    "jobs",
    JobState,
    json_columns=frozenset({"payload", "authorizing", "failure_context"}),
)
ARTIFACTS = Repository(Artifact, "artifacts")

# The image catalog (ADR-0092). A plain `Repository`: the publish/register state machine
# (defined → pending → registered, re-arming `pending_since`) is owned by the images publish
# service, not the `can_transition`-guarded `update_state`, so it is not a StatefulRepository.
# `provenance` is jsonb; `capabilities` is a Postgres text[] psycopg adapts from a list directly.
IMAGE_CATALOG = Repository(
    ImageCatalogEntry, "image_catalog", json_columns=frozenset({"provenance"})
)

# Accounting tables. COST_CLASS_COEFFICIENTS/QUOTAS upsert every non-key column;
# BUDGETS upserts only `limit_kcu` so a re-set_budget never clobbers `spent_kcu` (the
# DB-maintained running total). LEDGER is append-only with a DB-authoritative `ts`.
COST_CLASS_COEFFICIENTS = KeyedRepository(
    CostClassCoefficient, "cost_class_coefficients", "cost_class"
)
BUDGETS = KeyedRepository(Budget, "budgets", "project", update_columns=frozenset({"limit_kcu"}))
QUOTAS = KeyedRepository(Quota, "quotas", "project")
LEDGER = Repository(LedgerEntry, "ledger", server_generated=("ts",))

# The shapes catalog (ADR-0067). Keyed by `name`: the resolver calls `get`, the `shapes.*`
# tools call `upsert` / `list_all` / `delete`. `upsert` rewrites every non-key column (a
# re-set is a full redefinition of the preset). `updated_at` is the only server-generated
# column (the trigger maintains it); there is no `created_at`.
SYSTEM_SHAPES = KeyedRepository(SystemShape, "system_shapes", "name")
