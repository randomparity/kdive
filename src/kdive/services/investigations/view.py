"""Investigation read-model helpers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TypedDict
from uuid import UUID

from psycopg import AsyncConnection

from kdive.domain.lifecycle.records import Investigation
from kdive.serialization import JsonValue

_log = logging.getLogger(__name__)
type InvestigationListItem = Investigation | InvestigationRowError


class InvestigationAttachments(TypedDict):
    """Run and System ids attached to an Investigation."""

    runs: list[JsonValue]
    systems: list[JsonValue]


@dataclass(frozen=True, slots=True)
class InvestigationRowError:
    """A row that failed Investigation validation during list rendering."""

    object_id: object | None


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


def investigation_list_item(row: dict[str, object]) -> InvestigationListItem:
    """Validate a row for collection rendering, degrading invalid rows to a row error."""
    try:
        return Investigation.model_validate(row)
    except ValueError:
        object_id = row.get("id")
        _log.warning(
            "investigation %s violates the response invariant; degraded",
            object_id if object_id is not None else "<missing>",
            exc_info=True,
        )
        return InvestigationRowError(object_id)
