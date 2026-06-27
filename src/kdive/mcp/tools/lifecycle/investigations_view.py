"""Investigation envelope rendering helpers."""

from __future__ import annotations

import logging
from typing import TypedDict
from uuid import UUID

from psycopg import AsyncConnection

from kdive.domain.capacity.state import InvestigationState
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle.records import Investigation
from kdive.mcp.responses import ToolResponse
from kdive.serialization import JsonValue

_log = logging.getLogger(__name__)
type InvestigationListItem = Investigation | ToolResponse

_TERMINAL_INVESTIGATION = frozenset({InvestigationState.CLOSED, InvestigationState.ABANDONED})


class InvestigationAttachments(TypedDict):
    """Run and System ids attached to an Investigation envelope."""

    runs: list[JsonValue]
    systems: list[JsonValue]


async def attached_runs_and_systems(
    conn: AsyncConnection, investigation_id: UUID
) -> tuple[list[JsonValue], list[JsonValue]]:
    """Return ``(run_ids, distinct_system_ids)`` for an Investigation's attached Runs."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, system_id FROM runs WHERE investigation_id = %s ORDER BY created_at, id",
            (investigation_id,),
        )
        rows = await cur.fetchall()
    run_ids: list[JsonValue] = [str(run_id) for run_id, _ in rows]
    seen: set[str] = set()
    system_ids: list[JsonValue] = []
    for _, system_id in rows:
        sid = str(system_id)
        if sid not in seen:
            seen.add(sid)
            system_ids.append(sid)
    return run_ids, system_ids


async def attachments_for_investigations(
    conn: AsyncConnection, investigation_ids: list[UUID]
) -> dict[UUID, InvestigationAttachments]:
    """Batch-load attached Runs and distinct Systems for Investigation envelopes."""
    attachments: dict[UUID, InvestigationAttachments] = {
        uid: {"runs": [], "systems": []} for uid in investigation_ids
    }
    if not investigation_ids:
        return attachments
    seen_systems: dict[UUID, set[str]] = {uid: set() for uid in investigation_ids}
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT investigation_id, id, system_id FROM runs "
            "WHERE investigation_id = ANY(%s) ORDER BY investigation_id, created_at, id",
            (investigation_ids,),
        )
        rows = await cur.fetchall()
    for raw_investigation_id, run_id, system_id in rows:
        investigation_id = UUID(str(raw_investigation_id))
        attached = attachments[investigation_id]
        attached["runs"].append(str(run_id))
        sid = str(system_id)
        if sid not in seen_systems[investigation_id]:
            seen_systems[investigation_id].add(sid)
            attached["systems"].append(sid)
    return attachments


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


def investigation_list_item(row: dict[str, object]) -> InvestigationListItem:
    """Validate a row for collection rendering, degrading invalid rows to error envelopes."""
    try:
        return Investigation.model_validate(row)
    except ValueError:
        object_id = row.get("id")
        _log.warning(
            "investigation %s violates the response invariant; degraded",
            object_id if object_id is not None else "<missing>",
            exc_info=True,
        )
        return investigation_row_error(object_id)


def investigation_row_error(object_id: object | None) -> ToolResponse:
    """Return a degraded row envelope for an invalid Investigation row."""
    return ToolResponse.failure(
        str(object_id) if object_id is not None else "investigations.list",
        ErrorCategory.CONFIGURATION_ERROR,
    )
