"""Shared pass-budget accounting for provider host-state reaping lanes."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta

from psycopg import AsyncConnection

_log = logging.getLogger(__name__)

DEFAULT_LANE_BUDGET = timedelta(seconds=10)


@dataclass(frozen=True, slots=True)
class ReapLaneOutcome:
    """Counts reclaimed and budget-deferred candidates for one lane pass."""

    lane: str
    reaped: int
    budget_unattempted: int = 0


def lane_deadline(budget: timedelta) -> float:
    return time.monotonic() + budget.total_seconds()


def budget_unattempted(deadline: float, lane: str, *, remaining: int) -> int | None:
    if time.monotonic() < deadline:
        return None
    _log.info(
        "reconciler: %s lane ended its pass on the budget with %d candidate(s) unattempted; "
        "they are re-derived next pass",
        lane,
        remaining,
    )
    return remaining


async def database_epoch(conn: AsyncConnection) -> float:
    async with conn.cursor() as cur:
        await cur.execute("SELECT extract(epoch from now())")
        row = await cur.fetchone()
    return float(row[0]) if row is not None else 0.0
