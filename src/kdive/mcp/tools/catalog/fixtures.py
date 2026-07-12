"""``fixtures.list`` — provider-organized rootfs baseline catalog entries (ADR-0089 §6).

A plain authenticated read: the baseline rootfs inventory is provider-organized metadata, not
secret content, so there is no platform gate and no per-tool audit. It requires a valid token
(the verifier already gated the transport); the handler enforces token presence as defence in
depth. Each baseline rootfs entry flattens to ``{provider, name, arch}``. Keyset-paginated over
``(provider, name, arch)`` (ADR-0192), the same pattern ``images.list`` uses, so a large catalog
never returns unbounded rows inline.

The baseline rootfs catalog now lives only in the DB-backed ``image_catalog`` (ADR-0112): image
definitions were removed from code (the packaged ``seed_data`` YAML) and load from
``systems.toml`` via the inventory reconcile. This read reports the public catalog rows — the
same provider-organized inventory it reported before, now sourced from the reconciled DB instead
of packaged YAML. The published/registered detail view is the ``images list`` operator verb.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

from fastmcp import FastMCP
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.components.catalog import fixture_catalog_path_from_env, load_fixture_catalog
from kdive.domain.catalog.images import ImageVisibility
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.auth import current_context
from kdive.mcp.responses import JsonValue, ToolResponse
from kdive.mcp.schema.tool_payloads import ToolPayload
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT, InvalidCursor
from kdive.mcp.tools._common import clamp_list_limit as _clamp_list_limit
from kdive.mcp.tools._common import decode_cursor as _decode_cursor
from kdive.mcp.tools._common import encode_cursor as _encode_cursor
from kdive.mcp.tools._common import invalid_cursor_error as _invalid_cursor_error
from kdive.mcp.tools._common import paginate as _paginate

_OBJECT_ID = "fixtures"
_LIST_TOOL = "fixtures.list"
_LIST_TAG = "fixtures.list"
_VALIDATE_TOOL = "fixtures.validate"

_LIST_SQL = """
    SELECT provider, name, arch, volume
    FROM image_catalog
    WHERE visibility = %(public)s AND owner IS NULL
      AND (%(after)s::boolean IS FALSE
           OR (provider, name, arch) > (%(p)s, %(n)s, %(a)s))
    ORDER BY provider, name, arch
    LIMIT %(limit)s
"""


class _FixturesListPayload(ToolPayload):
    """Public payload for ``fixtures.list`` pagination."""

    limit: int = Field(
        default=DEFAULT_LIST_LIMIT,
        description=f"Maximum rows returned (capped at {MAX_LIST_LIMIT}).",
    )
    cursor: str | None = Field(
        default=None, description="Opaque continuation cursor from a prior page's next_cursor."
    )


async def _public_rows(
    pool: AsyncConnectionPool, *, limit: int, cursor: str | None
) -> tuple[list[JsonValue], bool, str | None] | ToolResponse:
    """Read one page of public catalog rows, flattened to ``{provider, name, arch, volume}``.

    Keyset-paginated over the ``(provider, name, arch)`` natural key (ADR-0192), mirroring
    ``images.list``: fetches one row past ``limit`` to derive the truncation flag and the
    next cursor from the last kept row's key. Returns an ``invalid_cursor``
    :class:`ToolResponse` in place of the tuple when ``cursor`` fails to decode.
    """
    capped = _clamp_list_limit(limit)
    after_parts: list[str] | None = None
    if cursor:
        try:
            after_parts = _decode_cursor(_LIST_TAG, cursor, arity=3)
        except InvalidCursor:
            return _invalid_cursor_error(_OBJECT_ID)
    params = {
        "public": ImageVisibility.PUBLIC.value,
        "after": after_parts is not None,
        "p": after_parts[0] if after_parts else "",
        "n": after_parts[1] if after_parts else "",
        "a": after_parts[2] if after_parts else "",
        "limit": capped + 1,
    }
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_LIST_SQL, params)
        rows = await cur.fetchall()
    kept, truncated = _paginate(rows, capped)
    next_cursor = (
        _encode_cursor(_LIST_TAG, (kept[-1]["provider"], kept[-1]["name"], kept[-1]["arch"]))
        if truncated and kept
        else None
    )
    fixtures: list[JsonValue] = [
        {
            "provider": row["provider"],
            "name": row["name"],
            "arch": row["arch"],
            "volume": row["volume"] or "",
        }
        for row in kept
    ]
    return fixtures, truncated, next_cursor


async def list_fixtures(
    pool: AsyncConnectionPool, *, limit: int = DEFAULT_LIST_LIMIT, cursor: str | None = None
) -> ToolResponse:
    """Return one page of public baseline catalog entries (provider, name, arch) from the DB.

    Keyset-paginated over ``(provider, name, arch)`` (ADR-0192): when ``data.truncated`` is
    true, pass ``data.next_cursor`` back as ``cursor`` for the next page.
    """
    page = await _public_rows(pool, limit=limit, cursor=cursor)
    if isinstance(page, ToolResponse):
        return page
    fixtures, truncated, next_cursor = page
    return ToolResponse.success(
        _OBJECT_ID,
        "ok",
        suggested_next_actions=[_LIST_TOOL],
        data={"fixtures": fixtures, "truncated": truncated, "next_cursor": next_cursor},
    )


async def validate_fixtures_tool(path: Path | None = None) -> ToolResponse:
    """Load the fixture catalog at the resolved path and report its profiles or an error.

    The operator-facing fail-fast for the ``KDIVE_FIXTURE_CATALOG_PATH`` override (ADR-0120):
    an operator runs this after mounting/overriding the catalog to confirm it loads and which
    profiles it advertises, instead of discovering a typo only deep in a later build. It attests
    the server process's resolved catalog.

    Args:
        path: An explicit catalog directory; ``None`` resolves ``KDIVE_FIXTURE_CATALOG_PATH``
            (or the packaged source-tree default).

    Returns:
        ``valid`` with ``{path, profiles:[{provider, name, arch}]}`` when the catalog loads,
        else a ``CONFIGURATION_ERROR`` failure carrying the resolved ``path`` and a bounded
        ``reason`` (the underlying exception type name — never the raw exception text or file
        body, which can quote operator-supplied content).
    """
    resolved = path or fixture_catalog_path_from_env()
    try:
        catalog = await asyncio.to_thread(load_fixture_catalog, resolved)
    except CategorizedError as exc:
        cause = exc.__cause__
        reason = type(cause).__name__ if cause is not None else type(exc).__name__
        return ToolResponse.failure(
            _OBJECT_ID,
            ErrorCategory.CONFIGURATION_ERROR,
            suggested_next_actions=[_VALIDATE_TOOL],
            data={"path": str(resolved), "reason": reason},
        )
    profiles: list[JsonValue] = sorted(
        ({"provider": p.provider, "name": p.name, "arch": p.arch} for p in catalog.profiles),
        key=lambda row: (row["provider"], row["name"], row["arch"]),
    )
    return ToolResponse.success(
        _OBJECT_ID,
        "valid",
        suggested_next_actions=[f"{_OBJECT_ID}.list"],
        data={"path": str(resolved), "profiles": profiles},
    )


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register ``fixtures.list`` and ``fixtures.validate`` on ``app``."""

    @app.tool(
        name=_LIST_TOOL,
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def fixtures_list(
        request: Annotated[
            _FixturesListPayload | None,
            Field(description="Fixture list pagination request; omit for the first page."),
        ] = None,
    ) -> ToolResponse:
        """List rootfs fixture catalog entries (provider, name, arch). Requires a valid token.

        Keyset-paginated: when ``data.truncated`` is true, pass ``data.next_cursor`` back as
        ``request.cursor`` for the next page.
        """
        current_context()
        payload = request or _FixturesListPayload()
        return await list_fixtures(pool, limit=payload.limit, cursor=payload.cursor)

    @app.tool(
        name=_VALIDATE_TOOL,
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def fixtures_validate() -> ToolResponse:
        """Validate the resolved fixture catalog and list its profiles. Requires a valid token."""
        current_context()
        return await validate_fixtures_tool()
