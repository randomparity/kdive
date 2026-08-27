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
_REDACTED_VMCORE_ID_SQL: LiteralString = (
    "SELECT id FROM artifacts "
    "WHERE owner_kind = 'runs' AND owner_id = %s "
    "AND object_key LIKE %s AND object_key LIKE %s "
    "ORDER BY created_at, id LIMIT 1"
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

_DEBUGINFO_REF_SQL: LiteralString = (
    "SELECT r.debuginfo_ref, r.build_ref, "
    "s.result->'artifact_versions'->>'vmlinux' AS version_id "
    "FROM runs r LEFT JOIN run_steps s ON s.run_id=r.id AND s.step='build' WHERE r.id = %s"
)

_KERNEL_REF_SQL: LiteralString = (
    "SELECT r.kernel_ref, r.build_ref, "
    "s.result->'artifact_versions'->>'kernel' AS version_id "
    "FROM runs r LEFT JOIN run_steps s ON s.run_id=r.id AND s.step='build' WHERE r.id = %s"
)

_RUN_FETCH_CONTEXT_SQL: LiteralString = (
    "SELECT r.project, r.system_id, r.debuginfo_ref, r.build_ref, "
    "s.result->'artifact_versions'->>'vmlinux' AS debuginfo_version_id "
    "FROM runs r LEFT JOIN run_steps s ON s.run_id=r.id AND s.step='build' WHERE r.id = %s"
)
_SYSTEM_PROJECT_SQL: LiteralString = "SELECT project FROM systems WHERE id = %s"


@dataclass(frozen=True, slots=True)
class RunFetchContext:
    """A Run's project, bound System id, and published vmlinux ref (ADR-0243)."""

    project: str
    system_id: UUID | None
    debuginfo_ref: str | None
    debuginfo_version_id: str | None
    reusable_build: bool


@dataclass(frozen=True, slots=True)
class ArtifactReadRef:
    """Object key and version pin; reusable refs require an immutable pin, while ``None`` is
    legacy."""

    key: str
    version_id: str | None


async def run_fetch_context(conn: AsyncConnection, run_id: UUID) -> RunFetchContext | None:
    """Return Run-owned raw-fetch context, normalizing an empty vmlinux ref to ``None``."""
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
        debuginfo_version_id=(
            str(row["debuginfo_version_id"])
            if isinstance(row["debuginfo_version_id"], str) and row["debuginfo_version_id"]
            else None
        ),
        reusable_build=isinstance(row["build_ref"], str) and bool(row["build_ref"]),
    )


async def system_project(conn: AsyncConnection, system_id: UUID) -> str | None:
    """Return a System's owning project, or ``None`` when the row is absent (ADR-0243)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_SYSTEM_PROJECT_SQL, (system_id,))
        row = await cur.fetchone()
    return None if row is None else str(row["project"])


async def raw_vmcore_key(conn: AsyncConnection, run_id: UUID) -> str | None:
    """Return a Run-owned raw vmcore key, excluding its ``-redacted`` sibling (ADR-0244)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            _RAW_VMCORE_KEY_SQL,
            (run_id, _RAW_VMCORE_KEY_LIKE, _REDACTED_VMCORE_LIKE),
        )
        row = await cur.fetchone()
    return None if row is None else str(row["object_key"])


async def redacted_vmcore_artifact_id(conn: AsyncConnection, run_id: UUID) -> str | None:
    """Return a Run-owned redacted vmcore id, or ``None`` when its sibling is absent (ADR-0466)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            _REDACTED_VMCORE_ID_SQL,
            (run_id, _RAW_VMCORE_KEY_LIKE, _REDACTED_VMCORE_LIKE),
        )
        row = await cur.fetchone()
    return None if row is None else str(row["id"])


async def raw_pcap_key(conn: AsyncConnection, run_id: UUID, artifact_id: UUID | None) -> str | None:
    """Return a Run-owned pcap by id, or the newest by ``(created_at, id)`` when id is ``None``."""
    async with conn.cursor(row_factory=dict_row) as cur:
        if artifact_id is not None:
            await cur.execute(_RAW_PCAP_KEY_BY_ID_SQL, (artifact_id, run_id))
        else:
            await cur.execute(_RAW_PCAP_NEWEST_KEY_SQL, (run_id,))
        row = await cur.fetchone()
    return None if row is None else str(row["object_key"])


async def effective_config_key(conn: AsyncConnection, run_id: UUID) -> str | None:
    """Return a Run's ``effective_config`` key for the debug-feature gate, or ``None``."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_EFFECTIVE_CONFIG_KEY_SQL, (run_id, _EFFECTIVE_CONFIG_KEY_LIKE))
        row = await cur.fetchone()
    return None if row is None else str(row["object_key"])


def debuginfo_ref_for_run_sync(conn: Connection, run_id: UUID) -> ArtifactReadRef | None:
    """Return a Run's vmlinux ref off the event loop; reusable builds require an immutable pin."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_DEBUGINFO_REF_SQL, (run_id,))
        row = cur.fetchone()
    if row is None:
        return None
    ref = row["debuginfo_ref"]
    if not isinstance(ref, str) or not ref:
        return None
    version = row["version_id"]
    if row["build_ref"] is not None and not version:
        raise RuntimeError("reusable vmlinux is missing its immutable object version")
    return ArtifactReadRef(ref, str(version) if isinstance(version, str) and version else None)


def kernel_ref_for_run_sync(conn: Connection, run_id: UUID) -> ArtifactReadRef | None:
    """Return a Run's kernel ref off the event loop; reusable builds require an immutable pin."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_KERNEL_REF_SQL, (run_id,))
        row = cur.fetchone()
    if row is None:
        return None
    ref = row["kernel_ref"]
    if not isinstance(ref, str) or not ref:
        return None
    version = row["version_id"]
    if row["build_ref"] is not None and not version:
        raise RuntimeError("reusable kernel is missing its immutable object version")
    return ArtifactReadRef(ref, str(version) if isinstance(version, str) and version else None)
