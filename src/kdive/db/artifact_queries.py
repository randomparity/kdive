"""Shared artifact lookup queries used across planes and MCP tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import LiteralString
from uuid import UUID

from psycopg import AsyncConnection, Connection
from psycopg.rows import dict_row

_RAW_VMCORE_KEY_SQL: LiteralString = (
    "SELECT object_key FROM artifacts "
    "WHERE owner_kind = 'systems' AND owner_id = %s "
    "AND object_key LIKE %s AND object_key NOT LIKE %s"
)
_RAW_VMCORE_KEY_LIKE = "%/vmcore-%"
_REDACTED_VMCORE_LIKE = "%-redacted"

_DEBUGINFO_REF_SQL: LiteralString = "SELECT debuginfo_ref FROM runs WHERE id = %s"

_RUN_FETCH_CONTEXT_SQL: LiteralString = (
    "SELECT project, system_id, debuginfo_ref FROM runs WHERE id = %s"
)
_SYSTEM_PROJECT_SQL: LiteralString = "SELECT project FROM systems WHERE id = %s"


@dataclass(frozen=True, slots=True)
class RunFetchContext:
    """A Run's project, bound System id, and published vmlinux ref (ADR-0243)."""

    project: str
    system_id: UUID | None
    debuginfo_ref: str | None


async def run_fetch_context(conn: AsyncConnection, run_id: UUID) -> RunFetchContext | None:
    """Return the Run's project, bound System id, and vmlinux ref, or ``None`` if absent.

    The fetch-raw egress addresses both assets through the Run: ``vmlinux`` is the Run's own
    ``debuginfo_ref`` and the raw ``vmcore`` is the core of the System the Run booted
    (``system_id``). ``debuginfo_ref`` normalizes an empty/NULL value to ``None`` so the caller
    treats "no vmlinux" uniformly.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_RUN_FETCH_CONTEXT_SQL, (run_id,))
        row = await cur.fetchone()
    if row is None:
        return None
    ref = row["debuginfo_ref"]
    return RunFetchContext(
        project=str(row["project"]),
        system_id=row["system_id"],
        debuginfo_ref=str(ref) if isinstance(ref, str) and ref else None,
    )


async def system_project(conn: AsyncConnection, system_id: UUID) -> str | None:
    """Return a System's owning project, or ``None`` when the row is absent (ADR-0243)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_SYSTEM_PROJECT_SQL, (system_id,))
        row = await cur.fetchone()
    return None if row is None else str(row["project"])


async def raw_vmcore_key(conn: AsyncConnection, system_id: UUID) -> str | None:
    """Return the System's raw ``vmcore-{method}`` object key, or ``None``."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            _RAW_VMCORE_KEY_SQL,
            (system_id, _RAW_VMCORE_KEY_LIKE, _REDACTED_VMCORE_LIKE),
        )
        row = await cur.fetchone()
    return None if row is None else str(row["object_key"])


def debuginfo_ref_for_run_sync(conn: Connection, run_id: UUID) -> str | None:
    """Return the Run's published debuginfo (vmlinux) object key, or ``None``.

    Sync because the gdb-MI attach seam runs off the event loop (``asyncio.to_thread``) and owns no
    async pool. ``None`` covers both an absent Run row and a row whose ``debuginfo_ref`` is NULL —
    the caller (the gdb-MI debuginfo resolver) treats both as ``no_debuginfo``.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_DEBUGINFO_REF_SQL, (run_id,))
        row = cur.fetchone()
    if row is None:
        return None
    ref = row["debuginfo_ref"]
    return str(ref) if isinstance(ref, str) and ref else None
