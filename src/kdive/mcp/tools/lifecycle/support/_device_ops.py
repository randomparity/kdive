"""Mutual exclusion between device hot-plug jobs and the System lifecycle tools (ADR-0823).

A plug or un-plug job holds the provider domain while it waits for the guest, so power, snapshot,
restore and reprovision refuse while one is queued or running. Each caller runs the probe inside
its own System advisory lock, as ``_active_snapshot_op`` does for snapshot jobs. Teardown is
exempt: its ``release_all`` must run so a stuck job cannot block reclaim.
"""

from __future__ import annotations

from uuid import UUID

from psycopg import AsyncConnection

from kdive.domain.capacity.state import JobState
from kdive.domain.errors import ErrorCategory
from kdive.domain.operations.jobs import JobKind
from kdive.mcp.responses import ToolResponse

DEVICE_JOB_KINDS: tuple[JobKind, ...] = (
    JobKind.PLUG_DEVICE,
    JobKind.UNPLUG_DEVICE,
    JobKind.FORCE_UNPLUG_DEVICE,
)
_ACTIVE_JOB_STATES: tuple[str, ...] = (JobState.QUEUED.value, JobState.RUNNING.value)

DEVICE_OP_IN_FLIGHT = "device_op_in_flight"


async def device_job_active(conn: AsyncConnection, system_id: UUID) -> bool:
    """Whether a plug or un-plug job is queued or running on the System."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM jobs WHERE payload->>'system_id' = %s AND kind = ANY(%s) "
            "AND state = ANY(%s) LIMIT 1",
            (str(system_id), [k.value for k in DEVICE_JOB_KINDS], list(_ACTIVE_JOB_STATES)),
        )
        return await cur.fetchone() is not None


def device_op_in_flight(system_id: str) -> ToolResponse:
    """The ``conflict`` every admission path returns while a device job is active."""
    return ToolResponse.failure(
        system_id,
        ErrorCategory.CONFLICT,
        detail="a device plug or un-plug is in progress on this System; wait for it to settle "
        "(systems.list_devices shows its state)",
        suggested_next_actions=["systems.list_devices", "systems.get"],
        data={"reason": DEVICE_OP_IN_FLIGHT},
    )


__all__ = ["DEVICE_JOB_KINDS", "DEVICE_OP_IN_FLIGHT", "device_job_active", "device_op_in_flight"]
