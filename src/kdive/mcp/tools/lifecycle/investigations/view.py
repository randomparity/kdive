"""MCP response rendering for Investigations."""

from __future__ import annotations

from uuid import UUID

from psycopg import AsyncConnection

from kdive.domain.capacity.state import InvestigationState
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle.records import Investigation
from kdive.mcp.responses import ToolResponse
from kdive.serialization import JsonValue
from kdive.services.investigations.view import (
    InvestigationAttachments,
    InvestigationRowError,
    attached_runs_and_systems,
    attachments_for_investigations,
    investigation_list_item,
)

_TERMINAL_INVESTIGATION = frozenset({InvestigationState.CLOSED, InvestigationState.ABANDONED})


def investigation_envelope(
    inv: Investigation, attachments: InvestigationAttachments
) -> ToolResponse:
    """Render an Investigation; every state is a non-failure status."""
    if inv.state in _TERMINAL_INVESTIGATION:
        actions = ["investigations.get"]
    else:
        actions = ["investigations.get", "investigations.close", "runs.create"]
    data: dict[str, JsonValue] = {
        "project": inv.project,
        "title": inv.title,
        "description": inv.description,
        "external_refs": [r.model_dump() for r in inv.external_refs],
        "state": inv.state.value,
        "last_run_at": inv.last_run_at.isoformat() if inv.last_run_at else None,
        "runs": attachments["runs"],
        "systems": attachments["systems"],
    }
    return ToolResponse.success(
        str(inv.id), inv.state.value, suggested_next_actions=actions, data=data
    )


async def envelope_for_investigation(conn: AsyncConnection, inv: Investigation) -> ToolResponse:
    """Load attachments and render a single Investigation envelope."""
    run_ids, system_ids = await attached_runs_and_systems(conn, inv.id)
    return investigation_envelope(inv, {"runs": run_ids, "systems": system_ids})


def investigation_row_error(object_id: object | None) -> ToolResponse:
    """Return a degraded row envelope for an invalid Investigation row."""
    return ToolResponse.failure(
        str(object_id) if object_id is not None else "investigations.list",
        ErrorCategory.CONFIGURATION_ERROR,
    )


def render_list_item(
    item: Investigation | InvestigationRowError,
    attachments: dict[UUID, InvestigationAttachments],
) -> ToolResponse:
    """Render one validated or degraded Investigation list item."""
    if isinstance(item, InvestigationRowError):
        return investigation_row_error(item.object_id)
    return investigation_envelope(item, attachments[item.id])


__all__ = [
    "attachments_for_investigations",
    "envelope_for_investigation",
    "investigation_envelope",
    "investigation_list_item",
    "investigation_row_error",
    "render_list_item",
]
