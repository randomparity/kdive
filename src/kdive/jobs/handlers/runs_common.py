"""Shared helpers for run job handlers."""

from __future__ import annotations

import logging
from uuid import UUID

from psycopg import AsyncConnection

_log = logging.getLogger(__name__)


async def abandon_run_step_best_effort(conn: AsyncConnection, run_id: UUID, step: str) -> None:
    from kdive.jobs.handlers import runs

    try:
        await runs.abandon_run_step(conn, run_id, step)
    except Exception:
        _log.warning(
            "failed to abandon %s step claim for run %s; preserving original failure",
            step,
            run_id,
            exc_info=True,
        )
