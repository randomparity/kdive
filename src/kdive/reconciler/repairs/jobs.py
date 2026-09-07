"""Job-row repair for the reconciler."""

from __future__ import annotations

import logging
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.domain.capacity.state import JobState, RunState
from kdive.domain.errors import ErrorCategory
from kdive.domain.operations.jobs import JobKind
from kdive.jobs.handlers.system_reclaim import (
    RetiredKeyBatchDeleter,
    reclaim_system_core_after_provider_teardown,
)
from kdive.jobs.payloads import PayloadValidationError, run_id_from_payload

_log = logging.getLogger(__name__)

_RUN_COMPENSATION_STATES = (RunState.CREATED, RunState.RUNNING)
_RUN_COMPENSATION_STATE_VALUES = tuple(state.value for state in _RUN_COMPENSATION_STATES)
_FAILED_JOB_STATE_VALUE = JobState.FAILED.value
_RUNNING_JOB_STATE_VALUE = JobState.RUNNING.value
_FAILED_RUN_STATE_VALUE = RunState.FAILED.value
_LEASE_EXPIRED_CATEGORY_VALUE = ErrorCategory.LEASE_EXPIRED.value
_TERMINAL_AUTHORITY_SYSTEM_REPAIR_LIMIT = 100

# Only commit_external_boot_authority_result may terminalize an authority-marked job
# (0122_external_boot_authority.sql:731), which writes the jobs row under SECURITY DEFINER.
# 0122 also fenced the generic finalizers, but this sweep is raw SQL and was never fenced --
# it was simply unreachable, because a marked job could not be claimed and so never held a
# lease that could lapse. 0127_reopen_external_boot_claim_lane.sql makes it reachable, so the
# predicate has to be explicit here: failing one of these jobs would drive its Run to failed
# from outside the commit that owns authority state.
_EXTERNAL_BOOT_AUTHORITY_MARKER = "external_boot_authority_v1"
_AUTHORITY_SYSTEM_MARKER = "authority_system_v1"


async def _repair_terminal_authority_system_provisions(conn: AsyncConnection) -> int:
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            "SELECT public.repair_terminal_authority_system_attempts(%s)",
            (_TERMINAL_AUTHORITY_SYSTEM_REPAIR_LIMIT,),
        )
        row = await cur.fetchone()
    if row is None:
        raise RuntimeError("authority System terminal provision repair returned no result")
    return int(row[0])


async def _authority_system_teardown_candidates(
    conn: AsyncConnection,
) -> list[tuple[UUID, int, UUID, int, str, str]]:
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            "SELECT * FROM public.list_authority_system_teardown_repairs(%s)",
            (_TERMINAL_AUTHORITY_SYSTEM_REPAIR_LIMIT,),
        )
        rows = await cur.fetchall()
    return [
        (authority_id, generation, system_id, sequence, journal_digest, receipt_digest)
        for authority_id, generation, system_id, sequence, journal_digest, receipt_digest in rows
    ]


async def _finalize_authority_system_teardown(
    conn: AsyncConnection,
    candidate: tuple[UUID, int, UUID, int, str, str],
) -> str:
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            "SELECT public.finalize_authority_system_teardown_repair(%s,%s,%s,%s,%s,%s)",
            candidate,
        )
        row = await cur.fetchone()
    if row is None:
        raise RuntimeError("authority System terminal teardown repair returned no result")
    return str(row[0])


async def repair_terminal_authority_system_attempts(
    conn: AsyncConnection, artifact_store: RetiredKeyBatchDeleter
) -> int:
    """Consume a bounded batch of durable authority-System terminal receipts.

    Provision receipts can commit entirely in SQL. A preactivation-absence receipt first runs the
    idempotent core cleanup; its exact SQL finalizer refuses success until the durable cleanup facts
    are absent. Per-System failures retain the candidate for a later pass without starving siblings.
    """
    repaired = await _repair_terminal_authority_system_provisions(conn)
    for candidate in await _authority_system_teardown_candidates(conn):
        authority_id, _generation, system_id, _sequence, _journal_digest, _receipt_digest = (
            candidate
        )
        try:
            await reclaim_system_core_after_provider_teardown(
                conn,
                artifact_store,
                system_id,
                reclaim_snapshot_ledger=True,
                discharge_mutation_obligations=True,
            )
            status = await _finalize_authority_system_teardown(conn, candidate)
        except Exception:  # noqa: BLE001 - retain this candidate and continue the bounded batch
            _log.warning(
                "reconciler: authority System teardown repair failed for attempt %s; "
                "retrying next pass",
                authority_id,
                exc_info=True,
            )
            continue
        if status == "applied":
            repaired += 1
        elif status == "cleanup-required":
            _log.warning(
                "reconciler: authority System teardown attempt %s still has core cleanup; "
                "retrying next pass",
                authority_id,
            )
        elif status == "superseded":
            _log.info(
                "reconciler: authority System teardown attempt %s was superseded", authority_id
            )
        elif status == "conflict":
            _log.error(
                "reconciler: authority System teardown attempt %s conflicts with consumed receipt",
                authority_id,
            )
        else:
            raise RuntimeError("authority System teardown repair returned an invalid status")
    return repaired


async def repair_abandoned_jobs(conn: AsyncConnection) -> int:
    """Dead-letter zombie jobs the worker can never reclaim, compensating their Run.

    Authority-marked jobs are skipped because their receipt-specific SQL commit or repair path
    exclusively owns terminalization. Writing one here would split the job transition from its
    authority ownership and receipt consumption.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id FROM jobs "
            "WHERE state = %s AND lease_expires_at < now() "
            "  AND attempt >= max_attempts "
            "  AND NOT (payload ? %s) "
            "  AND NOT (payload ? %s)",
            (
                _RUNNING_JOB_STATE_VALUE,
                _EXTERNAL_BOOT_AUTHORITY_MARKER,
                _AUTHORITY_SYSTEM_MARKER,
            ),
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
