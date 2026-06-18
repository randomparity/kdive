"""``fixtures.list`` — provider-organized rootfs baseline catalog entries (ADR-0089 §6).

A plain authenticated read: the baseline rootfs inventory is provider-organized metadata, not
secret content, so there is no platform gate and no per-tool audit. It requires a valid token
(the verifier already gated the transport); the handler enforces token presence as defence in
depth. Each baseline rootfs entry flattens to ``{provider, name, arch}``.

The baseline rootfs catalog now lives only in the DB-backed ``image_catalog`` (ADR-0112): image
definitions were removed from code (the packaged ``seed_data`` YAML) and load from
``systems.toml`` via the inventory reconcile. This read reports the public catalog rows — the
same provider-organized inventory it reported before, now sourced from the reconciled DB instead
of packaged YAML. The published/registered detail view is the ``images list`` operator verb.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastmcp import FastMCP
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.components.catalog import fixture_catalog_path_from_env, load_fixture_catalog
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.images import ImageVisibility
from kdive.mcp.auth import current_context
from kdive.mcp.responses import JsonValue, ToolResponse
from kdive.mcp.tools import _docmeta

_OBJECT_ID = "fixtures"
_VALIDATE_TOOL = "fixtures.validate"


async def _public_rows(pool: AsyncConnectionPool) -> list[JsonValue]:
    """Read the public catalog rows, flattened to ``{provider, name, arch}`` presence rows.

    Ordered by ``(provider, name, arch)`` so the listing is deterministic across passes.
    """
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT provider, name, arch, volume FROM image_catalog "
            "WHERE visibility = %s AND owner IS NULL "
            "ORDER BY provider, name, arch",
            (ImageVisibility.PUBLIC.value,),
        )
        rows = await cur.fetchall()
    return [
        {
            "provider": row["provider"],
            "name": row["name"],
            "arch": row["arch"],
            "volume": row["volume"] or "",
        }
        for row in rows
    ]


async def list_fixtures_tool(pool: AsyncConnectionPool) -> ToolResponse:
    """Return the public baseline catalog entries (provider, name, arch) from the DB."""
    return ToolResponse.success(_OBJECT_ID, "ok", data={"fixtures": await _public_rows(pool)})


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
        name="fixtures.list",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def fixtures_list() -> ToolResponse:
        """List rootfs fixture catalog entries (provider, name, arch). Requires a valid token."""
        current_context()
        return await list_fixtures_tool(pool)

    @app.tool(
        name=_VALIDATE_TOOL,
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def fixtures_validate() -> ToolResponse:
        """Validate the resolved fixture catalog and list its profiles. Requires a valid token."""
        current_context()
        return await validate_fixtures_tool()
