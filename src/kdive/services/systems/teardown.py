"""Shared System-locked external-boot teardown routing (ADRs 0620, 0816)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from hashlib import sha256
from typing import LiteralString
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.db.external_boot_activations import ExternalBootActivationRepository
from kdive.domain.capacity.state import JobState, SystemState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.external_boot_activation import ExternalBootActivation
from kdive.domain.lifecycle.records import System
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import queue
from kdive.jobs.payloads import Authorizing, TeardownPayload, dump_payload
from kdive.providers.core.resolver import ProviderResolver
from kdive.services.external_boot import (
    ExternalBootDenied,
    ExternalBootOperation,
    check_external_boot_admission,
)
from kdive.services.external_boot.payloads import build_external_boot_payload
from kdive.services.systems.authority_owned import _teardown_dedup_key

_LIVE_JOB_STATES = frozenset({JobState.QUEUED, JobState.RUNNING})
_AUTHORITY_MARKER = "external_boot_authority_v1"
_EXTERNAL_BOOT_ACTIVATIONS = ExternalBootActivationRepository()

_SYSTEM_TEARDOWN_AUTHORITY_SQL: LiteralString = (
    "SELECT activation_id, run_id, plan_identity, provider_kind, authority_instance "
    "FROM resolve_external_boot_system_teardown_dispatch_binding(%s)"
)

_WRITTEN_SINCE_SQL: LiteralString = "SELECT updated_at >= %s FROM jobs WHERE id = %s"


# The adapter records its existing response inside the transaction. A true flag skips
# a key already naming this job when replacing a settled job's payload.
type TeardownRecorder = Callable[[AsyncConnection, Job, bool], Awaitable[None]]


class TeardownRoutingDenied(Exception):
    """A deliberate pre-write denial that adapters may render without rolling back."""

    def __init__(self, error: CategorizedError) -> None:
        self.error = error
        super().__init__(str(error))


async def route_external_boot_teardown(
    conn: AsyncConnection,
    system: System,
    authorizing: Authorizing,
    resolver: ProviderResolver | None,
    *,
    on_recycle: Callable[[Job], Awaitable[None]] | None = None,
    recorder: TeardownRecorder | None = None,
) -> Job | None:
    """Route a System with any external-boot activation to its authority teardown, else ``None``.

    The caller owns the transaction and holds the System advisory lock; callbacks run in it.
    Shared by `systems.teardown` and break-glass
    `ops.force_teardown` (#3007): the worker refuses an ordinary teardown for such a System (#2966).
    A latest activation the stranded-activation settlement ended is read first, before the
    authority binding, and goes to the worker's ordinary teardown (ADR-0816 amendment, #3551).
    """
    activation = await _EXTERNAL_BOOT_ACTIVATIONS.get_latest_for_system(conn, system.id)
    if activation is None:
        return None
    settled = await _EXTERNAL_BOOT_ACTIVATIONS.settled_at(conn, activation.id)
    if settled is not None:
        return await _route_settled_teardown(
            conn, system, authorizing, settled, on_recycle=on_recycle, recorder=recorder
        )
    return await _enqueue_authority_teardown(
        conn,
        system,
        authorizing,
        activation,
        resolver,
        on_recycle=on_recycle,
        recorder=recorder,
    )


async def _route_settled_teardown(
    conn: AsyncConnection,
    system: System,
    authorizing: Authorizing,
    settled: datetime,
    *,
    on_recycle: Callable[[Job], Awaitable[None]] | None,
    recorder: TeardownRecorder | None,
) -> Job | None:
    """Send a settled System to its worker's ordinary teardown; ``None`` is the caller's own path.

    Each caller's ordinary path handles a missing or ordinary prior row and a `torn_down` or
    `reprovisioning` System, but they differ on an authority-marked row, so it is decided here: a
    live one, or one canceled after a worker claimed it and written since settlement, may still be
    running and replays; any other is replaced with the ordinary payload.
    """
    key = _teardown_dedup_key(system.id)
    prior = await queue.get_by_dedup_key(conn, key)
    if (
        prior is None
        or not _is_authority_marked(prior)
        or system.state in {SystemState.TORN_DOWN, SystemState.REPROVISIONING}
    ):
        return None
    if prior.state in _LIVE_JOB_STATES or (
        prior.state is JobState.CANCELED
        and prior.worker_id is not None
        and await _written_since(conn, prior.id, settled)
    ):
        return prior
    try:
        await check_external_boot_admission(
            conn, system.id, ExternalBootOperation.SYSTEM_TEARDOWN, project=system.project
        )
    except ExternalBootDenied as exc:
        raise TeardownRoutingDenied(exc) from exc
    job, changed = await queue.enqueue_with_status(
        conn,
        JobKind.TEARDOWN,
        TeardownPayload(system_id=str(system.id)),
        authorizing,
        key,
        recycle=queue.JobRecyclePolicy.TERMINAL_OR_CANCELED,
    )
    if changed and on_recycle is not None:
        await on_recycle(job)
    if recorder is not None:
        await recorder(conn, job, True)
    return job


async def _written_since(conn: AsyncConnection, job_id: UUID, moment: datetime) -> bool:
    cursor = await conn.execute(_WRITTEN_SINCE_SQL, (moment, job_id))
    row = await cursor.fetchone()
    return row is not None and bool(row[0])


def _is_authority_marked(job: Job) -> bool:
    return _AUTHORITY_MARKER in job.payload or "authority_system_v1" in job.payload


def _is_settled_ordinary_teardown(job: Job | None) -> bool:
    """Whether ``job`` is an unmarked teardown no worker attempt can still be running.

    A canceled row qualifies only if no worker ever claimed it: `jobs.cancel` is cooperative, and
    a recycled row restarts at attempt 1, which a still-running canceled attempt would match.
    """
    return (
        job is not None
        and not _is_authority_marked(job)
        and (
            job.state is JobState.FAILED
            or (job.state is JobState.CANCELED and job.worker_id is None)
        )
    )


async def _enqueue_authority_teardown(
    conn: AsyncConnection,
    system: System,
    authorizing: Authorizing,
    activation: ExternalBootActivation,
    resolver: ProviderResolver | None,
    *,
    on_recycle: Callable[[Job], Awaitable[None]] | None,
    recorder: TeardownRecorder | None,
) -> Job:
    """Route every durable external-boot history through its exact authority marker."""
    if resolver is None:
        raise TeardownRoutingDenied(
            CategorizedError(
                "external-boot teardown requires the configured authority resolver",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"reason": "external_boot_teardown_authority_unresolved"},
            )
        )
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_SYSTEM_TEARDOWN_AUTHORITY_SQL, (system.id,))
        bindings = await cur.fetchall()
    if len(bindings) != 1 or UUID(str(bindings[0]["activation_id"])) != activation.id:
        raise TeardownRoutingDenied(
            CategorizedError(
                "the newest external-boot activation has no unambiguous authority route",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"reason": "external_boot_teardown_authority_unresolved"},
            )
        )
    prior = await queue.get_by_dedup_key(conn, _teardown_dedup_key(system.id))
    # The worker refuses an ordinary teardown for external-boot history before any provider call,
    # so a settled ordinary job is replaced by the authority teardown (ADR-0620 amendment, #2966).
    replaces_ordinary = _is_settled_ordinary_teardown(prior)
    if prior is not None and not replaces_ordinary:
        marker = prior.payload.get(_AUTHORITY_MARKER)
        if not isinstance(marker, dict) or marker.get("activation_id") != str(activation.id):
            raise TeardownRoutingDenied(
                CategorizedError(
                    "an ordinary teardown job cannot be replayed for external-boot history",
                    category=ErrorCategory.CONFLICT,
                    details={"reason": "ordinary_teardown_fenced_by_external_boot"},
                )
            )
        final_attempt_running = (
            prior.state is JobState.RUNNING and prior.attempt >= prior.max_attempts
        )
        if prior.state is not JobState.FAILED and not final_attempt_running:
            return prior
    operation_identity = (
        "sha256:"
        + sha256(
            f"{activation.id}\0system-teardown\0{activation.plan_identity}\0{_teardown_dedup_key(system.id)}".encode()
        ).hexdigest()
    )
    binding = bindings[0]
    try:
        kind, payload = await build_external_boot_payload(
            conn,
            activation_id=activation.id,
            purpose="teardown",
            operation="teardown",
            provider_kind=str(binding["provider_kind"]),
            authority_instance=str(binding["authority_instance"]),
            operation_identity=operation_identity,
            resolver=resolver,
        )
    except CategorizedError as exc:
        if prior is not None and prior.state is JobState.RUNNING:
            return prior
        raise TeardownRoutingDenied(
            CategorizedError(
                f"the external-boot teardown authority cannot be dispatched: {exc}",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"reason": "external_boot_teardown_authority_unresolved"},
            )
        ) from exc
    if (
        prior is not None
        and not replaces_ordinary
        and dump_payload(kind, payload).get(_AUTHORITY_MARKER)
        != prior.payload.get(_AUTHORITY_MARKER)
    ):
        return prior
    # A failed authority teardown, or one whose final attempt's lease lapsed, with the identical
    # marker is re-run (ADR-0620 amendments). The lapse is judged only by enqueue's UPDATE on the
    # database clock, so a final attempt that is still live comes back unchanged and replays.
    if prior is None:
        recycle = queue.JobRecyclePolicy.NEVER
    elif replaces_ordinary:
        # The row is failed, or canceled and never claimed, as read under the System lock.
        recycle = queue.JobRecyclePolicy.TERMINAL_OR_CANCELED
    else:
        recycle = queue.JobRecyclePolicy.FAILED_OR_LAPSED_EXHAUSTED
    job, changed = await queue.enqueue_with_status(
        conn,
        kind,
        payload,
        authorizing,
        _teardown_dedup_key(system.id),
        recycle=recycle,
    )
    # A recycle keeps the first enqueuer as `job.authorizing` (ADR-0447, ADR-0435, ADR-0620); the
    # requester is audited here. Break-glass is already recorded in `platform_audit_log`, and
    # its platform admin need not hold the System's project.
    if prior is not None and changed and on_recycle is not None:
        await on_recycle(job)
    # Only ordinary-row replacement may already have recorded this same job handle.
    if recorder is not None:
        await recorder(conn, job, replaces_ordinary)
    return job
