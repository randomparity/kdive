"""``fixtures.validate`` — operator fail-fast for the on-disk fixture profile catalog (ADR-0120).

A plain authenticated read: the fixture profile catalog is provider-organized metadata, not secret
content, so there is no platform gate and no per-tool audit. It requires a valid token (the
verifier already gated the transport); the handler enforces token presence as defence in depth.

This namespace reads the **filesystem** profile catalog resolved from
``KDIVE_FIXTURE_CATALOG_PATH``. The DB-backed baseline rootfs rows are a different source
entirely, and listing them now belongs to ``images.list(scope="public_baseline")`` (ADR-0465) —
the projection this module's retired ``fixtures.list`` used to serve.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool

from kdive.components.catalog import fixture_catalog_path_from_env, load_fixture_catalog
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.auth import current_context
from kdive.mcp.responses import JsonValue, ToolResponse
from kdive.mcp.tools import _docmeta

_OBJECT_ID = "fixtures"
_VALIDATE_TOOL = "fixtures.validate"


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
    # Order the typed profiles, then project. Sorting the projected rows instead keys off a
    # `JsonValue`, which is not known to be a mapping and so cannot be subscripted.
    ordered = sorted(catalog.profiles, key=lambda p: (p.provider, p.name, p.arch))
    profiles: list[JsonValue] = [
        {"provider": p.provider, "name": p.name, "arch": p.arch} for p in ordered
    ]
    return ToolResponse.success(
        _OBJECT_ID,
        "valid",
        suggested_next_actions=["images.list"],
        data={"path": str(resolved), "profiles": profiles},
    )


def register(app: FastMCP, _pool: AsyncConnectionPool) -> None:
    """Register ``fixtures.validate`` on ``app``.

    Takes the shared pool to satisfy the plane-registrar signature; the fixture profile catalog
    is an on-disk read (ADR-0120), so this plane touches no database.
    """

    @app.tool(
        name=_VALIDATE_TOOL,
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def fixtures_validate() -> ToolResponse:
        """Validate the resolved fixture catalog and list its profiles. Requires a valid token."""
        current_context()
        return await validate_fixtures_tool()
