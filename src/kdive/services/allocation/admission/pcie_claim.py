"""Read active PCIe claims for admission and placement (ADR-0068).

Grant-time callers read claims under the Resource lock before matching and persistence.
Placement uses a preliminary snapshot; admission rechecks under that lock.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from psycopg import AsyncConnection

from kdive.domain.lifecycle.rules import NON_TERMINAL_ALLOCATION_STATE_VALUES
from kdive.domain.pcie import PCIeClaim

if TYPE_CHECKING:
    from uuid import UUID


async def active_claims(conn: AsyncConnection, resource_id: UUID) -> list[PCIeClaim]:
    """Return the host's occupancy set: every device held by a non-terminal allocation.

    Unions the ``pcie_claim`` snapshots of the host's allocations in a non-terminal state
    (the same set admission's capacity check counts), so a ``releasing`` allocation's
    device still reads as occupied and a ``released``/``expired``/``failed`` one's does
    not. The caller resolving + claiming a device must run this under the per-Resource
    lock for the read to be authoritative.
    """
    claims: list[PCIeClaim] = []
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT pcie_claim FROM allocations "
            "WHERE resource_id = %s AND state = ANY(%s) AND pcie_claim <> '[]'::jsonb",
            (resource_id, list(NON_TERMINAL_ALLOCATION_STATE_VALUES)),
        )
        rows = await cur.fetchall()
    for row in rows:
        for held in row[0]:
            claims.append(
                PCIeClaim(bdf=held["bdf"], vendor_id=held["vendor_id"], device_id=held["device_id"])
            )
    return claims
