"""Authorized artifact listing queries shared by MCP tool surfaces."""

from __future__ import annotations

from typing import NamedTuple
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.domain.catalog.artifacts import Sensitivity
from kdive.log import bind_context
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role

# The bounded, ordered Run-scoped console manifest cap surfaced on ``runs.get`` (ADR-0279, #935).
# A chatty multi-hour Run can correlate many console parts; the manifest returns the newest
# CONSOLE_MANIFEST_MAX with a total/truncated disclosure so ``runs.get`` stays token-bounded.
CONSOLE_MANIFEST_MAX = 100

_RUN_CONSOLE_MANIFEST_SQL = (
    "SELECT id, object_key, created_at FROM artifacts "
    "WHERE run_id = %s AND owner_kind = 'systems' AND sensitivity = %s "
    "ORDER BY created_at DESC, object_key DESC LIMIT %s"
)
_RUN_CONSOLE_COUNT_SQL = (
    "SELECT count(*) AS n FROM artifacts "
    "WHERE run_id = %s AND owner_kind = 'systems' AND sensitivity = %s"
)


class ConsoleManifest(NamedTuple):
    """A Run's correlated console artifacts (newest-first) plus the full correlated count.

    ``entries`` is at most ``CONSOLE_MANIFEST_MAX`` ``{artifact_id, object_key, created_at}`` dicts;
    ``total`` is the unbounded count, so the caller can disclose truncation when ``total`` exceeds
    ``len(entries)``.
    """

    entries: list[dict[str, str]]
    total: int


async def list_run_console_artifacts(
    conn: AsyncConnection, run_id: UUID | str, *, limit: int = CONSOLE_MANIFEST_MAX
) -> ConsoleManifest:
    """Return the Run-correlated System-owned console artifacts, newest-first (ADR-0279, #935).

    Ordered ``(created_at DESC, object_key DESC)`` — a total order, since every part one
    ``console_rotate`` job seals shares a transaction ``created_at`` and the zero-padded
    ``console-part-<gen>-<index>`` key is the within-batch tiebreak. The boot-evidence snapshot
    (``console-<run_id>``) and every attributed part are included; an uncorrelated part on the same
    System is excluded by the ``run_id`` filter. The caller passes its already-open connection (the
    Run is project-checked before this runs, so the entries carry no cross-project signal).
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_RUN_CONSOLE_MANIFEST_SQL, (run_id, Sensitivity.REDACTED.value, limit))
        rows = await cur.fetchall()
        await cur.execute(_RUN_CONSOLE_COUNT_SQL, (run_id, Sensitivity.REDACTED.value))
        count_row = await cur.fetchone()
    entries = [
        {
            "artifact_id": str(row["id"]),
            "object_key": str(row["object_key"]),
            "created_at": row["created_at"].isoformat(),
        }
        for row in rows
    ]
    total = int(count_row["n"]) if count_row is not None else len(entries)
    return ConsoleManifest(entries=entries, total=total)


_LIST_REDACTED_SYSTEM_SQL = (
    "SELECT id, object_key FROM artifacts "
    "WHERE owner_kind = 'systems' AND owner_id = %s AND sensitivity = %s "
    "ORDER BY created_at DESC"
)
_LIST_REDACTED_RUN_SQL = (
    "SELECT id, object_key FROM artifacts "
    "WHERE owner_kind = 'runs' AND owner_id = %s AND sensitivity = %s "
    "ORDER BY created_at DESC"
)
_SYSTEM_PROJECT_SQL = "SELECT project FROM systems WHERE id = %s"
_RUN_PROJECT_SQL = "SELECT project FROM runs WHERE id = %s"


class RedactedArtifact(NamedTuple):
    id: str
    object_key: str


async def list_redacted_system_artifacts(
    pool: AsyncConnectionPool, ctx: RequestContext, *, system_id: str
) -> list[RedactedArtifact]:
    """Return redacted artifact rows for an authorized System; absent systems return empty."""
    try:
        uid = UUID(system_id)
    except ValueError:
        return []
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_SYSTEM_PROJECT_SQL, (uid,))
            owner = await cur.fetchone()
            if owner is None or owner["project"] not in ctx.projects:
                return []
            require_role(ctx, owner["project"], Role.VIEWER)
            await cur.execute(_LIST_REDACTED_SYSTEM_SQL, (uid, Sensitivity.REDACTED.value))
            rows = await cur.fetchall()
    return [RedactedArtifact(id=str(row["id"]), object_key=str(row["object_key"])) for row in rows]


async def list_redacted_run_artifacts(
    pool: AsyncConnectionPool, ctx: RequestContext, *, run_id: str
) -> list[RedactedArtifact]:
    """Return redacted artifact rows owned by an authorized Run; absent Runs return empty.

    The per-Run analog of :func:`list_redacted_system_artifacts` (ADR-0244): vmcore cores are
    Run-owned (``owner_kind='runs'``), so the redacted dmesg derivative is listed by Run id.
    """
    try:
        uid = UUID(run_id)
    except ValueError:
        return []
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_RUN_PROJECT_SQL, (uid,))
            owner = await cur.fetchone()
            if owner is None or owner["project"] not in ctx.projects:
                return []
            require_role(ctx, owner["project"], Role.VIEWER)
            await cur.execute(_LIST_REDACTED_RUN_SQL, (uid, Sensitivity.REDACTED.value))
            rows = await cur.fetchall()
    return [RedactedArtifact(id=str(row["id"]), object_key=str(row["object_key"])) for row in rows]
