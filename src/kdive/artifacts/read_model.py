"""Artifact read-model helpers shared by services, MCP, workers, and feature packages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import LiteralString
from uuid import UUID

from psycopg import AsyncConnection, Connection
from psycopg.rows import dict_row

RUN_ARTIFACT_NAMES = frozenset({"effective_config", "kernel", "initrd", "vmlinux"})
SYSTEM_ARTIFACT_NAMES = frozenset({"rootfs"})

_RAW_VMCORE_KEY_SQL: LiteralString = (
    "SELECT object_key FROM artifacts "
    "WHERE owner_kind = 'runs' AND owner_id = %s "
    "AND object_key LIKE %s AND object_key NOT LIKE %s"
)
_RAW_VMCORE_KEY_LIKE = "%/vmcore-%"
_REDACTED_VMCORE_LIKE = "%-redacted"

_RAW_PCAP_KEY_BY_ID_SQL: LiteralString = (
    "SELECT object_key FROM artifacts "
    "WHERE id = %s AND owner_kind = 'runs' AND owner_id = %s AND retention_class = 'pcap'"
)
_RAW_PCAP_NEWEST_KEY_SQL: LiteralString = (
    "SELECT object_key FROM artifacts "
    "WHERE owner_kind = 'runs' AND owner_id = %s AND retention_class = 'pcap' "
    "ORDER BY created_at DESC, id DESC LIMIT 1"
)

_EFFECTIVE_CONFIG_KEY_SQL: LiteralString = (
    "SELECT object_key FROM artifacts "
    "WHERE owner_kind = 'runs' AND owner_id = %s AND object_key LIKE %s LIMIT 1"
)
_EFFECTIVE_CONFIG_KEY_LIKE = "%/effective_config"

_DEBUGINFO_REF_SQL: LiteralString = "SELECT debuginfo_ref FROM runs WHERE id = %s"

_KERNEL_REF_SQL: LiteralString = "SELECT kernel_ref FROM runs WHERE id = %s"

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


async def raw_vmcore_key(conn: AsyncConnection, run_id: UUID) -> str | None:
    """Return the Run's raw ``vmcore-{method}`` object key, or ``None`` (ADR-0244).

    Cores are owned by the Run that crashed (``owner_kind='runs'``); the redacted dmesg sibling
    (``-redacted``) is excluded so only the raw core resolves.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            _RAW_VMCORE_KEY_SQL,
            (run_id, _RAW_VMCORE_KEY_LIKE, _REDACTED_VMCORE_LIKE),
        )
        row = await cur.fetchone()
    return None if row is None else str(row["object_key"])


async def raw_pcap_key(conn: AsyncConnection, run_id: UUID, artifact_id: UUID | None) -> str | None:
    """Object key of a Run-owned pcap: the exact one by ``artifact_id``, or the newest (ADR-0384).

    A Run may own several pcaps (one per ``capture_traffic`` job). ``artifact_id`` selects one and
    validates it belongs to this Run (``owner_kind='runs'``, ``retention_class='pcap'``); ``None``
    resolves the newest. Returns ``None`` for an absent id, a cross-Run id, or a Run with no pcap.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        if artifact_id is not None:
            await cur.execute(_RAW_PCAP_KEY_BY_ID_SQL, (artifact_id, run_id))
        else:
            await cur.execute(_RAW_PCAP_NEWEST_KEY_SQL, (run_id,))
        row = await cur.fetchone()
    return None if row is None else str(row["object_key"])


async def effective_config_key(conn: AsyncConnection, run_id: UUID) -> str | None:
    """Return the Run's uploaded ``effective_config`` object key, or ``None`` (ADR-0318).

    The agent's ``.config`` is a Run-owned (``owner_kind='runs'``) upload accepted but not
    validated; its object key is read here for the debug-feature config gate.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_EFFECTIVE_CONFIG_KEY_SQL, (run_id, _EFFECTIVE_CONFIG_KEY_LIKE))
        row = await cur.fetchone()
    return None if row is None else str(row["object_key"])


def debuginfo_ref_for_run_sync(conn: Connection, run_id: UUID) -> str | None:
    """Return the Run's published debuginfo (vmlinux) object key, or ``None``.

    Sync because the gdb-MI attach seam runs off the event loop (``asyncio.to_thread``) and owns no
    async pool. ``None`` covers both an absent Run row and a row whose ``debuginfo_ref`` is NULL;
    the caller (the gdb-MI debuginfo resolver) treats both as ``no_debuginfo``.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_DEBUGINFO_REF_SQL, (run_id,))
        row = cur.fetchone()
    if row is None:
        return None
    ref = row["debuginfo_ref"]
    return str(ref) if isinstance(ref, str) and ref else None


def kernel_ref_for_run_sync(conn: Connection, run_id: UUID) -> str | None:
    """Return the Run's published combined kernel+modules tar object key, or ``None``.

    Sync for the same reason as :func:`debuginfo_ref_for_run_sync` (the gdb-MI ops run off the
    event loop). ``None`` covers both an absent Run row and a NULL ``kernel_ref``; the caller (the
    module-debuginfo resolver) treats both as ``no_module_debuginfo``.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_KERNEL_REF_SQL, (run_id,))
        row = cur.fetchone()
    if row is None:
        return None
    ref = row["kernel_ref"]
    return str(ref) if isinstance(ref, str) and ref else None
