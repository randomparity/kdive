"""Enqueue worker-owned repair for stranded external-boot activations (ADR-0596)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, LiteralString, cast
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import ValidationError

from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import queue
from kdive.jobs.handlers.external_boot.admission import build_external_boot_payload
from kdive.jobs.models import ExternalBootAuthorityMarkerV1
from kdive.jobs.payloads import (
    Authorizing,
    BootPayload,
    PayloadValidationError,
    TeardownPayload,
    load_payload,
)
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.external_boot_authority.protocol import Purpose

_log = logging.getLogger(__name__)

type RepairOperation = Literal[
    "activate", "recover", "resolve-conflict", "release", "cleanup", "teardown"
]


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


_CANDIDATE_SQL = {
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
        WHERE a.state IN ('recovered', 'abandoned', 'recovery_conflict', 'recovery_failed')
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


async def _candidates(conn: AsyncConnection, lane: str) -> tuple[_Candidate, ...]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            cast(LiteralString, _CANDIDATE_SQL[lane] + " ORDER BY a.id LIMIT %s"),
            (_REPAIR_BATCH,),
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


async def _source_job(conn: AsyncConnection, candidate: _Candidate) -> Job | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM jobs WHERE kind = ANY(%s) "
            "AND payload #>> '{external_boot_authority_v1,activation_id}' = %s "
            "AND payload #>> '{external_boot_authority_v1,system_id}' = %s "
            "AND payload #>> '{external_boot_authority_v1,run_id}' = %s "
            "AND payload #>> '{external_boot_authority_v1,plan_identity}' = %s "
            "AND authorizing ->> 'project' = %s "
            "ORDER BY created_at DESC, id DESC LIMIT %s",
            (
                [JobKind.BOOT.value, JobKind.TEARDOWN.value],
                str(candidate.activation_id),
                str(candidate.system_id),
                str(candidate.run_id),
                candidate.plan_identity,
                candidate.project,
                _SOURCE_JOB_SCAN_LIMIT,
            ),
        )
        rows = await cur.fetchall()
    for row in rows:
        job = Job.model_validate(row)
        try:
            if job.kind is JobKind.BOOT:
                payload = load_payload(job, BootPayload)
            elif job.kind is JobKind.TEARDOWN:
                payload = load_payload(job, TeardownPayload)
            else:
                continue
            marker = payload.external_boot_authority_v1
            Authorizing.model_validate(job.authorizing)
        except PayloadValidationError, ValidationError:
            continue
        if marker is not None and (
            marker.activation_id,
            marker.system_id,
            marker.run_id,
            marker.plan_identity,
            job.authorizing["project"],
        ) == (
            candidate.activation_id,
            candidate.system_id,
            candidate.run_id,
            candidate.plan_identity,
            candidate.project,
        ):
            return job
    return None


async def _live_successor_exists(
    conn: AsyncConnection, candidate: _Candidate, *, authority_instance: str
) -> bool:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM jobs WHERE kind = ANY(%s) "
            "AND payload #>> '{external_boot_authority_v1,activation_id}' = %s "
            "AND payload #>> '{external_boot_authority_v1,system_id}' = %s "
            "AND payload #>> '{external_boot_authority_v1,run_id}' = %s "
            "AND payload #>> '{external_boot_authority_v1,plan_identity}' = %s "
            "AND payload #>> '{external_boot_authority_v1,operation}' = %s "
            "AND payload #>> '{external_boot_authority_v1,authority_instance}' = %s "
            "AND authorizing ->> 'project' = %s "
            "AND (state = 'queued' OR (state = 'running' AND "
            "(lease_expires_at >= now() OR attempt < max_attempts))) "
            "ORDER BY created_at DESC, id DESC LIMIT %s",
            (
                [JobKind.BOOT.value, JobKind.TEARDOWN.value],
                str(candidate.activation_id),
                str(candidate.system_id),
                str(candidate.run_id),
                candidate.plan_identity,
                candidate.operation,
                authority_instance,
                candidate.project,
                _SOURCE_JOB_SCAN_LIMIT,
            ),
        )
        rows = await cur.fetchall()
    for row in rows:
        job = Job.model_validate(row)
        try:
            model = load_payload(job, BootPayload if job.kind is JobKind.BOOT else TeardownPayload)
        except PayloadValidationError:
            continue
        marker = model.external_boot_authority_v1
        if marker is not None and marker.operation == candidate.operation:
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
) -> bool:
    if await _live_successor_exists(conn, candidate, authority_instance=authority_instance):
        return False
    source = await _source_job(conn, candidate)
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
    lane: Literal["activation", "recovery", "release", "cleanup"],
    resolver: ProviderResolver,
    authority_instance: str,
) -> int:
    """Enqueue deterministic successors for one post-prepared repair lane."""
    repaired = 0
    for candidate in await _candidates(conn, lane):
        try:
            repaired += await _enqueue_candidate(
                conn,
                candidate,
                resolver=resolver,
                authority_instance=authority_instance,
            )
        except Exception:
            # Provider and durable payload exceptions may contain host identifiers. The lane's
            # bounded operation label is enough for telemetry; raw diagnostics stay private.
            _log.warning(
                "reconciler: external-boot %s candidate repair failed", candidate.operation
            )
    return repaired


__all__ = ["repair_external_boot_lane"]
