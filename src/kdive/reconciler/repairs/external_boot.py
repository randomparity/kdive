"""Enqueue worker-owned repair for stranded external-boot activations (ADR-0596)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, LiteralString, cast
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import ValidationError

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import queue
from kdive.jobs.external_boot_models import ExternalBootAuthorityMarkerV1
from kdive.jobs.payloads import (
    Authorizing,
    BootPayload,
    PayloadValidationError,
    TeardownPayload,
    load_payload,
)
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.external_boot_authority.protocol import Purpose
from kdive.services.external_boot.payloads import build_external_boot_payload

_log = logging.getLogger(__name__)

type RepairOperation = Literal[
    "activate", "recover", "resolve-conflict", "release", "cleanup", "teardown"
]


type RepairLane = Literal["activation", "recovery", "release", "cleanup"]
type RepairCursors = dict[RepairLane, UUID]


@dataclass(frozen=True, slots=True)
class _Candidate:
    activation_id: UUID
    system_id: UUID
    run_id: UUID
    plan_identity: str
    project: str
    operation: RepairOperation


_REPAIR_BATCH = 100
_SOURCE_JOB_SCAN_LIMIT = 100
_JOB_LOOKUP_TIMEOUT_MS = 2_000


_CANDIDATE_SQL: dict[RepairLane, str] = {
    "activation": """
        SELECT a.id AS activation_id, a.system_id, a.run_id, a.plan_identity, system.project,
               'activate' AS operation
        FROM external_boot_activations a
        JOIN systems system ON system.id = a.system_id
        WHERE a.state = 'prepared'
           OR (a.state = 'activating' AND a.activation_readiness_deadline < now())
    """,
    "recovery": """
        SELECT a.id AS activation_id, a.system_id, a.run_id, a.plan_identity, system.project,
               CASE WHEN attempt.recovery_basis = 'pre_recovery'
                    THEN 'resolve-conflict' ELSE 'recover' END AS operation
        FROM external_boot_activations a
        JOIN external_boot_recovery_attempts attempt
          ON attempt.activation_id = a.id AND attempt.attempt_id = a.current_attempt_id
        JOIN systems system ON system.id = a.system_id
        WHERE a.state = 'recovering'
          AND attempt.state = 'recovering'
          AND attempt.recovery_readiness_deadline < now()
    """,
    "release": """
        SELECT a.id AS activation_id, a.system_id, a.run_id, a.plan_identity, system.project,
               'release' AS operation
        FROM external_boot_activations a
        JOIN external_boot_reservations reservation ON reservation.activation_id = a.id
        JOIN systems system ON system.id = a.system_id
        LEFT JOIN external_boot_reservation_releases released ON released.activation_id = a.id
        WHERE a.state = 'recovered'
          AND NOT a.cleanup_complete AND reservation.state = 'ready'
          AND released.activation_id IS NULL
    """,
    "cleanup": """
        SELECT a.id AS activation_id, a.system_id, a.run_id, a.plan_identity, system.project,
               CASE WHEN a.state IN ('recovered', 'abandoned')
                    THEN 'cleanup' ELSE 'teardown' END AS operation
        FROM external_boot_activations a
        JOIN external_boot_reservation_releases released ON released.activation_id = a.id
        JOIN systems system ON system.id = a.system_id
        WHERE a.state IN ('recovered', 'abandoned', 'recovery_conflict', 'recovery_failed')
          AND NOT a.cleanup_complete
          AND (a.state IN ('recovered', 'abandoned') OR system.state = 'failed')
    """,
}


async def _candidates(
    conn: AsyncConnection, lane: RepairLane, *, after: UUID | None = None
) -> tuple[_Candidate, ...]:
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            cast(
                LiteralString,
                "SELECT candidate.* FROM ("
                + _CANDIDATE_SQL[lane]
                + ") AS candidate WHERE (%s::uuid IS NULL OR candidate.activation_id > %s) "
                "ORDER BY candidate.activation_id LIMIT %s",
            ),
            (after, after, _REPAIR_BATCH),
        )
        rows = await cur.fetchall()
    return tuple(
        _Candidate(
            activation_id=row["activation_id"],
            system_id=row["system_id"],
            run_id=row["run_id"],
            plan_identity=row["plan_identity"],
            project=row["project"],
            operation=row["operation"],
        )
        for row in rows
    )


async def _candidate_jobs(
    conn: AsyncConnection,
    candidates: tuple[_Candidate, ...],
    *,
    live_only: bool,
    authority_instance: str,
) -> tuple[Job, ...]:
    if not candidates:
        return ()
    candidate_values = ", ".join(
        "(%s::uuid, %s::uuid, %s::uuid, %s::text, %s::text, %s::text, %s::text)"
        for _candidate in candidates
    )
    parameters: list[object] = []
    for candidate in candidates:
        parameters.extend(
            (
                candidate.activation_id,
                candidate.system_id,
                candidate.run_id,
                candidate.plan_identity,
                candidate.project,
                candidate.operation,
                authority_instance,
            )
        )
    live_predicate = (
        "AND j.payload #>> '{external_boot_authority_v1,operation}' = candidate.operation "
        "AND j.payload #>> '{external_boot_authority_v1,authority_instance}' "
        "    = candidate.authority_instance "
        "AND (j.state = 'queued' OR (j.state = 'running' AND "
        "(j.lease_expires_at >= now() OR j.attempt < j.max_attempts))) "
        if live_only
        else ""
    )
    parameters.extend(([JobKind.BOOT.value, JobKind.TEARDOWN.value], _SOURCE_JOB_SCAN_LIMIT))
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (str(_JOB_LOOKUP_TIMEOUT_MS),),
        )
        await cur.execute(
            "WITH candidate (activation_id, system_id, run_id, plan_identity, project, "
            "operation, authority_instance) AS (VALUES " + candidate_values + "), ranked AS ("
            "SELECT j.id, row_number() OVER (PARTITION BY candidate.activation_id "
            "ORDER BY j.created_at DESC, j.id DESC) AS candidate_rank "
            "FROM candidate JOIN jobs j ON "
            "j.payload #>> '{external_boot_authority_v1,activation_id}' "
            "    = candidate.activation_id::text "
            "AND j.payload #>> '{external_boot_authority_v1,system_id}' "
            "    = candidate.system_id::text "
            "AND j.payload #>> '{external_boot_authority_v1,run_id}' = candidate.run_id::text "
            "AND j.payload #>> '{external_boot_authority_v1,plan_identity}' "
            "    = candidate.plan_identity "
            "AND j.authorizing ->> 'project' = candidate.project "
            "WHERE j.kind = ANY(%s) "
            + live_predicate
            + ") SELECT jobs.* FROM ranked JOIN jobs USING (id) "
            "WHERE candidate_rank <= %s ORDER BY jobs.created_at DESC, jobs.id DESC",
            parameters,
        )
        rows = await cur.fetchall()
    jobs: list[Job] = []
    for row in rows:
        try:
            jobs.append(Job.model_validate(row))
        except ValidationError:
            continue
    return tuple(jobs)


def _source_job(candidate: _Candidate, jobs: tuple[Job, ...]) -> Job | None:
    for job in jobs:
        try:
            payload_type = BootPayload if job.kind is JobKind.BOOT else TeardownPayload
            payload = load_payload(job, payload_type)
            marker = payload.external_boot_authority_v1
            authorizing = Authorizing.model_validate(job.authorizing)
        except PayloadValidationError, ValidationError:
            continue
        if marker is not None and (
            marker.activation_id,
            marker.system_id,
            marker.run_id,
            marker.plan_identity,
            authorizing.project,
        ) == (
            candidate.activation_id,
            candidate.system_id,
            candidate.run_id,
            candidate.plan_identity,
            candidate.project,
        ):
            return job
    return None


def _live_successor_exists(
    candidate: _Candidate, jobs: tuple[Job, ...], *, authority_instance: str
) -> bool:
    for job in jobs:
        try:
            model = load_payload(job, BootPayload if job.kind is JobKind.BOOT else TeardownPayload)
            authorizing = Authorizing.model_validate(job.authorizing)
        except PayloadValidationError, ValidationError:
            continue
        marker = model.external_boot_authority_v1
        if marker is not None and (
            marker.activation_id,
            marker.system_id,
            marker.run_id,
            marker.plan_identity,
            marker.operation,
            marker.authority_instance,
            authorizing.project,
        ) == (
            candidate.activation_id,
            candidate.system_id,
            candidate.run_id,
            candidate.plan_identity,
            candidate.operation,
            authority_instance,
            candidate.project,
        ):
            return True
    return False


def _purpose(operation: RepairOperation) -> Purpose:
    if operation == "cleanup":
        return "release"
    return operation


async def _enqueue_candidate(
    conn: AsyncConnection,
    candidate: _Candidate,
    *,
    resolver: ProviderResolver,
    authority_instance: str,
    source_jobs: tuple[Job, ...],
    live_jobs: tuple[Job, ...],
) -> bool:
    if _live_successor_exists(candidate, live_jobs, authority_instance=authority_instance):
        return False
    source = _source_job(candidate, source_jobs)
    if source is None:
        _log.warning(
            "reconciler: external-boot %s candidate has no validated source job",
            candidate.operation,
        )
        return False
    marker = ExternalBootAuthorityMarkerV1.model_validate(
        source.payload["external_boot_authority_v1"]
    )
    operation_identity = f"repair:{candidate.activation_id}:{candidate.operation}:{source.id}"
    kind, payload = await build_external_boot_payload(
        conn,
        activation_id=candidate.activation_id,
        purpose=_purpose(candidate.operation),
        operation=candidate.operation,
        provider_kind=marker.provider_kind,
        authority_instance=authority_instance,
        operation_identity=operation_identity,
        resolver=resolver,
        expected_observed_composite=(
            marker.expected_observed_composite
            if candidate.operation == "resolve-conflict"
            else None
        ),
    )
    _job, inserted = await queue.enqueue_with_status(
        conn,
        kind,
        payload,
        source.authorizing,
        f"external-boot:{operation_identity}",
    )
    return inserted


async def repair_external_boot_lane(
    conn: AsyncConnection,
    *,
    lane: RepairLane,
    resolver: ProviderResolver,
    authority_instance: str,
    cursors: RepairCursors | None = None,
) -> int:
    """Inspect at most one page, retaining per-lane progress in the caller's map (ADR-0839).

    Omitted state is one-shot; repeat standalone callers supply the same map. A nonempty page
    advances after each ordinary outcome, while cancellation retries the interrupted candidate.
    """
    if cursors is None:
        cursors = {}
    repaired = 0
    candidates = await _candidates(conn, lane, after=cursors.get(lane))
    if not candidates and lane in cursors:
        candidates = await _candidates(conn, lane)
    if not candidates:
        cursors.pop(lane, None)
        return 0
    source_jobs = await _candidate_jobs(
        conn, candidates, live_only=False, authority_instance=authority_instance
    )
    live_jobs = await _candidate_jobs(
        conn, candidates, live_only=True, authority_instance=authority_instance
    )
    for candidate in candidates:
        try:
            async with conn.transaction():
                inserted = await _enqueue_candidate(
                    conn,
                    candidate,
                    resolver=resolver,
                    authority_instance=authority_instance,
                    source_jobs=source_jobs,
                    live_jobs=live_jobs,
                )
            repaired += inserted
        except Exception as error:
            # Messages and tracebacks may contain provider identities or durable payloads.
            category = (
                error.category
                if isinstance(error, CategorizedError)
                else ErrorCategory.INFRASTRUCTURE_FAILURE
            )
            _log.warning(
                "reconciler: external-boot %s candidate repair failed "
                "activation_id=%s exception=%s category=%s",
                candidate.operation,
                candidate.activation_id,
                type(error).__name__[:80],
                category.value,
            )
        cursors[lane] = candidate.activation_id
    return repaired


__all__ = ["repair_external_boot_lane"]
