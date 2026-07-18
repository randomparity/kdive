"""Authorized artifact listing queries shared by MCP tool surfaces."""

from __future__ import annotations

from datetime import datetime
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


# The System-scoped list is keyset-paginated over ``(created_at, id) DESC`` like ``runs.list``
# (ADR-0192, ADR-0374). A System accumulates a redacted artifact per boot/rotation across every
# Run, so an uncapped list can blow the tool-result token budget (#1238). ``DEFAULT`` bounds the
# page when the caller names no limit; the seek clause is appended only for a continuation page.
DEFAULT_SYSTEM_LIST_LIMIT = 50
_LIST_REDACTED_SYSTEM_SQL = (
    "SELECT id, object_key, created_at FROM artifacts "
    "WHERE owner_kind = 'systems' AND owner_id = %s AND sensitivity = %s"
)
_LIST_REDACTED_SYSTEM_SEEK = " AND (created_at, id) < (%s, %s)"
_LIST_REDACTED_SYSTEM_ORDER = " ORDER BY created_at DESC, id DESC LIMIT %s"
_LIST_REDACTED_RUN_SQL = (
    "SELECT id, object_key FROM artifacts "
    "WHERE owner_kind = 'runs' AND owner_id = %s AND sensitivity = %s "
    "ORDER BY created_at DESC"
)
# The newest console artifact correlated to a Run (ADR-0374, #1238): same ``(created_at,
# object_key) DESC`` total order as the ADR-0279 manifest, so this is ``manifest.entries[0]``
# without loading the manifest. The ``owner_kind='systems'`` + ``run_id`` filter matches the boot
# snapshot (``console-<run_id>``) and every attributed rotating part.
_RUN_LATEST_CONSOLE_SQL = (
    "SELECT id FROM artifacts "
    "WHERE run_id = %s AND owner_kind = 'systems' AND sensitivity = %s "
    "ORDER BY created_at DESC, object_key DESC LIMIT 1"
)
_SYSTEM_PROJECT_SQL = "SELECT project FROM systems WHERE id = %s"
_RUN_PROJECT_SQL = "SELECT project FROM runs WHERE id = %s"


class RedactedArtifact(NamedTuple):
    id: str
    object_key: str


class SystemArtifactPage(NamedTuple):
    """One keyset page of a System's redacted artifacts (ADR-0374).

    ``items`` is the kept page (at most the requested limit, newest-first); ``truncated`` is
    ``True`` when a further page exists; ``next_key`` is the last kept row's ``(created_at, id)``
    for the caller to encode into a continuation cursor (``None`` when not truncated).
    """

    items: list[RedactedArtifact]
    truncated: bool
    next_key: tuple[datetime, UUID] | None


async def list_redacted_system_artifacts(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    system_id: str,
    limit: int = DEFAULT_SYSTEM_LIST_LIMIT,
    after: tuple[datetime, UUID] | None = None,
) -> SystemArtifactPage:
    """Return one keyset page of an authorized System's redacted artifacts, newest-first.

    Keyset-paginated over ``(created_at, id) DESC`` (ADR-0192/0374): the caller passes an already
    clamped ``limit`` and an optional ``after`` boundary (the prior page's ``next_key``); a
    non-visible, absent, or malformed System returns an empty, non-truncated page. Fetches one row
    past ``limit`` to detect truncation without a count query.
    """
    try:
        uid = UUID(system_id)
    except ValueError:
        return SystemArtifactPage([], False, None)
    fetch = max(1, limit) + 1
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_SYSTEM_PROJECT_SQL, (uid,))
            owner = await cur.fetchone()
            if owner is None or owner["project"] not in ctx.projects:
                return SystemArtifactPage([], False, None)
            require_role(ctx, owner["project"], Role.VIEWER)
            params: list[object] = [uid, Sensitivity.REDACTED.value]
            query = _LIST_REDACTED_SYSTEM_SQL
            if after is not None:
                query += _LIST_REDACTED_SYSTEM_SEEK
                params.extend(after)
            query += _LIST_REDACTED_SYSTEM_ORDER
            params.append(fetch)
            await cur.execute(query, tuple(params))
            rows = await cur.fetchall()
    truncated = len(rows) > max(1, limit)
    kept = rows[: max(1, limit)]
    items = [RedactedArtifact(id=str(row["id"]), object_key=str(row["object_key"])) for row in kept]
    next_key = (kept[-1]["created_at"], kept[-1]["id"]) if truncated and kept else None
    return SystemArtifactPage(items=items, truncated=truncated, next_key=next_key)


async def latest_run_console_artifact_id(conn: AsyncConnection, run_id: UUID | str) -> str | None:
    """Return the newest Run-correlated console artifact id, or ``None`` (ADR-0374, #1238).

    The ``refs.latest_console`` shortcut on ``runs.get``: a single indexed ``LIMIT 1`` read that
    resolves the newest console artifact (boot snapshot or rotating part) without loading the
    opt-in manifest. The caller passes its already-open, project-checked connection (mirrors
    :func:`list_run_console_artifacts`), so the id carries no cross-project signal.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_RUN_LATEST_CONSOLE_SQL, (run_id, Sensitivity.REDACTED.value))
        row = await cur.fetchone()
    return str(row["id"]) if row is not None else None


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
