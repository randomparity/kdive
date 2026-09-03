"""Job-row repair for the reconciler."""

from __future__ import annotations

import logging

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.domain.capacity.state import JobState, RunState
from kdive.domain.errors import ErrorCategory
from kdive.domain.operations.jobs import JobKind
from kdive.jobs.payloads import PayloadValidationError, run_id_from_payload

_log = logging.getLogger(__name__)

_RUN_COMPENSATION_STATES = (RunState.CREATED, RunState.RUNNING)
_RUN_COMPENSATION_STATE_VALUES = tuple(state.value for state in _RUN_COMPENSATION_STATES)
_FAILED_JOB_STATE_VALUE = JobState.FAILED.value
_RUNNING_JOB_STATE_VALUE = JobState.RUNNING.value
_FAILED_RUN_STATE_VALUE = RunState.FAILED.value
_LEASE_EXPIRED_CATEGORY_VALUE = ErrorCategory.LEASE_EXPIRED.value

# Only commit_external_boot_authority_result may terminalize an authority-marked job
# (0122_external_boot_authority.sql:731), which writes the jobs row under SECURITY DEFINER.
# 0122 also fenced the generic finalizers, but this sweep is raw SQL and was never fenced --
# it was simply unreachable, because a marked job could not be claimed and so never held a
# lease that could lapse. 0127_reopen_external_boot_claim_lane.sql makes it reachable, so the
# predicate has to be explicit here: failing one of these jobs would drive its Run to failed
# from outside the commit that owns authority state.
_EXTERNAL_BOOT_AUTHORITY_MARKER = "external_boot_authority_v1"


async def repair_abandoned_jobs(conn: AsyncConnection) -> int:
    """Dead-letter zombie jobs the worker can never reclaim, compensating their Run.

    Authority-marked external-boot jobs are skipped: only
    ``commit_external_boot_authority_result`` may terminalize one. Such a job whose worker
    died therefore lingers ``running`` with a lapsed lease until #2203 adds the reconciler
    detection lane that routes it to the authority path instead of writing to it here.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id FROM jobs "
            "WHERE state = %s AND lease_expires_at < now() "
            "  AND attempt >= max_attempts "
            "  AND NOT (payload ? %s)",
            (_RUNNING_JOB_STATE_VALUE, _EXTERNAL_BOOT_AUTHORITY_MARKER),
        )
        zombie_ids = [row["id"] for row in await cur.fetchall()]
    swept = 0
    for job_id in zombie_ids:
        async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "UPDATE jobs SET state = %s, error_category = %s "
                "WHERE id = %s AND state = %s RETURNING kind, payload",
                (
                    _FAILED_JOB_STATE_VALUE,
                    _LEASE_EXPIRED_CATEGORY_VALUE,
                    job_id,
                    _RUNNING_JOB_STATE_VALUE,
                ),
            )
            row = await cur.fetchone()
            if row is None:
                continue
            try:
                run_id = run_id_from_payload(JobKind(row["kind"]), row["payload"])
            except PayloadValidationError as exc:
                _log.warning(
                    "reconciler: abandoned job %s has invalid payload; "
                    "skipping Run compensation: %s",
                    job_id,
                    exc,
                )
                run_id = None
            if run_id is not None:
                await cur.execute(
                    "UPDATE runs SET state = %s, failure_category = %s "
                    "WHERE id = %s AND state = ANY(%s)",
                    (
                        _FAILED_RUN_STATE_VALUE,
                        _LEASE_EXPIRED_CATEGORY_VALUE,
                        run_id,
                        list(_RUN_COMPENSATION_STATE_VALUES),
                    ),
                )
        swept += 1
        _log.info("reconciler: abandoned job %s -> failed (lease_expired)", job_id)
    return swept
