"""FastMCP registration for the ``investigations.*`` tool surface."""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.domain.capacity.state import InvestigationState
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.schema.tool_payloads import ToolPayload
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT
from kdive.mcp.tools.lifecycle.investigations.common import (
    DESCRIPTION_MAX,
    TITLE_MAX,
    ExternalRefInput,
    ExternalRefKey,
)
from kdive.mcp.tools.lifecycle.investigations.lifecycle import (
    close_investigation,
    open_investigation,
)
from kdive.mcp.tools.lifecycle.investigations.metadata import (
    link_external_ref,
    set_investigation,
    unlink_external_ref,
)
from kdive.mcp.tools.lifecycle.investigations.read import (
    get_investigation,
    list_investigations,
)


class _InvestigationsListPayload(ToolPayload):
    """Public payload for ``investigations.list`` filters and pagination."""

    project: str | None = Field(
        default=None, description="Restrict to one project you can view; omit for all."
    )
    state: InvestigationState | None = Field(
        default=None, description="Filter by state (open/active/closed/abandoned)."
    )
    limit: int = Field(
        default=DEFAULT_LIST_LIMIT,
        description=f"Maximum rows returned (capped at {MAX_LIST_LIMIT}).",
    )
    cursor: str | None = Field(
        default=None, description="Opaque continuation cursor from a prior page's next_cursor."
    )


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the `investigations.*` tools on ``app``, bound to ``pool``."""
    _register_investigations_open(app, pool)
    _register_investigations_get(app, pool)
    _register_investigations_close(app, pool)
    _register_investigations_link(app, pool)
    _register_investigations_unlink(app, pool)
    _register_investigations_set(app, pool)
    _register_investigations_list(app, pool)


def _register_investigations_open(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="investigations.open",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def investigations_open(
        project: Annotated[str, Field(description="Project to create the Investigation under.")],
        title: Annotated[str, Field(description=f"Human-readable title (1..={TITLE_MAX} chars).")],
        description: Annotated[
            str | None,
            Field(
                description=(
                    f"Optional free-form description for reporting (<={DESCRIPTION_MAX} chars)."
                )
            ),
        ] = None,
        external_refs: Annotated[
            list[ExternalRefInput] | None,
            Field(description="Optional external tracker refs (each with tracker, id, url)."),
        ] = None,
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior envelope."),
        ] = None,
    ) -> ToolResponse:
        """Open an investigation."""
        return await open_investigation(
            pool,
            current_context(),
            project=project,
            title=title,
            description=description,
            external_refs=external_refs,
            idempotency_key=idempotency_key,
        )


def _register_investigations_get(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="investigations.get",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def investigations_get(
        investigation_id: Annotated[str, Field(description="The Investigation to render.")],
    ) -> ToolResponse:
        """Return one investigation."""
        return await get_investigation(pool, current_context(), investigation_id)


def _register_investigations_close(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="investigations.close",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def investigations_close(
        investigation_id: Annotated[
            str, Field(description="The Investigation to drive to closed.")
        ],
    ) -> ToolResponse:
        """Close an investigation."""
        return await close_investigation(pool, current_context(), investigation_id)


def _register_investigations_link(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="investigations.link",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def investigations_link(
        investigation_id: Annotated[str, Field(description="The Investigation to add the ref to.")],
        ref: Annotated[
            ExternalRefInput,
            Field(description="External ref to upsert, with tracker, id, and url."),
        ],
    ) -> ToolResponse:
        """Link an external tracker ref to an Investigation."""
        return await link_external_ref(pool, current_context(), investigation_id, ref)


def _register_investigations_unlink(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="investigations.unlink",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def investigations_unlink(
        investigation_id: Annotated[
            str, Field(description="The Investigation to remove the ref from.")
        ],
        ref: Annotated[
            ExternalRefKey,
            Field(description="Ref to remove; only tracker and id are used as the key."),
        ],
    ) -> ToolResponse:
        """Remove an external tracker ref from an Investigation."""
        return await unlink_external_ref(pool, current_context(), investigation_id, ref)


def _register_investigations_set(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="investigations.set",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def investigations_set(
        investigation_id: Annotated[str, Field(description="The Investigation to edit.")],
        title: Annotated[
            str | None,
            Field(description=f"New title (1..={TITLE_MAX} chars); omit to leave unchanged."),
        ] = None,
        description: Annotated[
            str | None,
            Field(
                description=(
                    f'New description (<={DESCRIPTION_MAX}); "" clears it; omit to leave unchanged.'
                )
            ),
        ] = None,
    ) -> ToolResponse:
        """Edit a non-terminal Investigation's title and/or free-form description."""
        return await set_investigation(
            pool, current_context(), investigation_id, title=title, description=description
        )


def _register_investigations_list(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="investigations.list",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def investigations_list(
        request: Annotated[
            _InvestigationsListPayload | None,
            Field(description="Investigation list filters and pagination request."),
        ] = None,
    ) -> ToolResponse:
        """List the Investigations you can view, newest-first, for reporting.

        Keyset-paginated: when ``data.truncated`` is true, pass ``data.next_cursor`` back as
        ``cursor`` for the next page.
        """
        payload = request or _InvestigationsListPayload()
        return await list_investigations(
            pool,
            current_context(),
            project=payload.project,
            state=payload.state.value if payload.state is not None else None,
            limit=payload.limit,
            cursor=payload.cursor,
        )
