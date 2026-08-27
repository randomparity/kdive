"""Connection-scoped operations over the durable ``jobs`` queue (ADR-0018, ADR-0533, ADR-0550).

``enqueue`` admits a job idempotently on ``dedup_key``; ``dequeue`` claims the oldest
eligible job with ``FOR UPDATE SKIP LOCKED``, charging an attempt and reclaiming a
lapsed lease; ``heartbeat`` renews a lease; ``complete`` and ``fail`` finalize a
claimed job. Every worker write goes through a credential-bound database function
that derives the active incarnation and fences the exact charged attempt. Each
function wraps its statements in ``conn.transaction()`` so it self-commits on any
connection, and all assume READ COMMITTED (psycopg's default).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from kdive.domain.capacity.state import JobState
from kdive.domain.errors import ErrorCategory
from kdive.domain.operations.jobs import (
    DEFAULT_JOB_DISPATCH_LANE,
    RETIRED_JOB_KINDS,
    SYSTEM_FAILING_JOB_KINDS,
    Job,
    JobAuthorizing,
    JobKind,
    dispatch_lane_for_kind,
)
from kdive.jobs.payloads import (
    ActivePayloadModel,
    Authorizing,
    dump_authorizing,
    dump_payload,
)

_log = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_LEASE = timedelta(minutes=5)
DEFAULT_DISPATCH_LANES = (DEFAULT_JOB_DISPATCH_LANE,)


async def enqueue(
    conn: AsyncConnection,
    kind: JobKind,
    payload: ActivePayloadModel,
    authorizing: Authorizing | JobAuthorizing,
    dedup_key: str,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    recycle_terminal: bool = False,
    recycle_canceled: bool = False,
) -> Job:
    """Admit a job idempotently, returning the row for ``dedup_key``.

    Recycling resets eligible terminal rows to a fresh queued attempt with the new payload and
    kind-derived dispatch lane. It preserves slot metadata (kind, authorizing principal, and
    ``max_attempts``), never replaces an in-flight row, and does not resurrect canceled work unless
    explicitly requested. ``created_at`` uses the wall clock so recycled jobs return to the back of
    the queue even when the caller's transaction waited on a lock (ADR-0447, ADR-0550).

    Raises:
        ValueError: ``max_attempts < 1`` (a job that ``dequeue`` could never claim).
        ValueError: ``recycle_canceled`` without ``recycle_terminal``.
        ValueError: ``kind`` is a retired historical kind without an active handler.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
    if recycle_canceled and not recycle_terminal:
        raise ValueError("recycle_canceled requires recycle_terminal")
    if kind in RETIRED_JOB_KINDS:
        raise ValueError(f"job kind {kind.value!r} is retired and cannot be enqueued")
    dispatch_lane = dispatch_lane_for_kind(kind)
    payload_json = dump_payload(kind, payload)
    authorizing = dump_authorizing(authorizing)
    recycled_id: UUID | None = None
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "INSERT INTO jobs "
            "(kind, dispatch_lane, payload, state, max_attempts, authorizing, dedup_key, "
            " created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, clock_timestamp()) "
            "ON CONFLICT (dedup_key) DO NOTHING",
            (
                kind,
                dispatch_lane,
                Jsonb(payload_json),
                JobState.QUEUED.value,
                max_attempts,
                Jsonb(authorizing),
                dedup_key,
            ),
        )
        if recycle_terminal:
            recyclable = [JobState.FAILED.value, JobState.SUCCEEDED.value]
            if recycle_canceled:
                recyclable.append(JobState.CANCELED.value)
            await cur.execute(
                "UPDATE jobs SET state = %s, payload = %s, attempt = 0, worker_id = NULL, "
                "    lease_expires_at = NULL, heartbeat_at = NULL, error_category = NULL, "
                "    result_ref = NULL, failure_context = '{}'::jsonb, "
                "    dispatch_lane = %s, created_at = clock_timestamp() "
                "WHERE dedup_key = %s AND state = ANY(%s) "
                "RETURNING id",
                (
                    JobState.QUEUED.value,
                    Jsonb(payload_json),
                    dispatch_lane,
                    dedup_key,
                    recyclable,
                ),
            )
            if (recycled := await cur.fetchone()) is not None:
                recycled_id = recycled["id"]
        await cur.execute("SELECT * FROM jobs WHERE dedup_key = %s", (dedup_key,))
        row = await cur.fetchone()
    # This records an attempted recycle; an outer transaction may still roll it back.
    if recycled_id is not None:
        _log.info(
            "recycled terminal job %s (kind %s, dedup_key %s) to a fresh queued attempt",
            recycled_id,
            kind.value,
            dedup_key,
        )
    if row is None:  # Invariant: we just inserted the row, or it already existed.
        raise RuntimeError(f"enqueue found no job for dedup_key {dedup_key!r}")
    return Job.model_validate(row)


async def get_by_dedup_key(conn: AsyncConnection, dedup_key: str) -> Job | None:
    """Return the job for ``dedup_key`` (the unique natural key), or ``None``.

    A read-only lookup on the ``jobs_dedup_key_key`` UNIQUE column — at most one row.
    Used to reach a System's terminal ``provision`` job (``f"{allocation_id}:provision"``)
    from the admission path so a retry can surface the original redacted reason (ADR-0149).
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM jobs WHERE dedup_key = %s", (dedup_key,))
        row = await cur.fetchone()
    return None if row is None else Job.model_validate(row)


async def dequeue(
    conn: AsyncConnection,
    worker_id: str,
    *,
    incarnation_credential: SecretStr,
    lease: timedelta = DEFAULT_LEASE,
    accepted_lanes: Sequence[str] = DEFAULT_DISPATCH_LANES,
) -> Job | None:
    """Claim the oldest eligible job for ``worker_id``, charging an attempt.

    Eligible: ``queued``, or ``running`` with a lapsed lease (an abandoned job), and
    ``attempt < max_attempts``. The single ``UPDATE`` sets ``running``/``worker_id``/
    lease/``heartbeat_at`` and ``attempt = attempt + 1`` (charging the claim bounds
    retries across worker death). ``FOR UPDATE SKIP LOCKED`` lets parallel workers
    claim disjoint rows without blocking. ``now()`` is the database clock, so no
    worker clocks need to agree.

    ``accepted_lanes`` is the worker's explicit dispatch boundary. A worker claims only
    queued/lapsed jobs whose persisted lane is in this set, so provider- or pool-specific
    workers do not acquire work they cannot execute.

    ``incarnation_credential`` is authority-minted for this exact worker. The guarded
    database function derives the incarnation from its hash and claims only when it is active,
    matches ``worker_id``, and uses the fixed current fence protocol.

    A ``capture_traffic`` retry remains ineligible while any prior supervised operation lacks
    complete provider quiescence, closed publication, or disposed spool evidence. Refusal does not
    charge an attempt or clear the current operation link; startup recovery must close the whole
    operation product first.

    ``lease`` is one PostgreSQL interval applied to ``clock_timestamp()`` captured by the database
    after the blocking incarnation fence and immediately before this claim. The computed deadline
    must be after that reference and no more than one hour later. SQLSTATE ``22023`` is raised
    before any job mutation when it is outside that elapsed bound; retry with an interval whose
    computed deadline is valid.

    ``ORDER BY created_at`` is FIFO over *when the attempt was queued*, not when the row was first
    inserted: :func:`enqueue`'s ``recycle_terminal`` re-dates ``created_at`` (ADR-0447), so a
    revived job queues behind the work admitted while it was settled instead of preempting it.

    Returns:
        The claimed :class:`Job`, or ``None`` when nothing is eligible for the accepted lanes.
    """
    if not accepted_lanes:
        return None
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM public.claim_worker_job("
            "%s, sha256(convert_to(%s, 'UTF8')), %s, %s::text[])",
            (
                worker_id,
                incarnation_credential.get_secret_value(),
                lease,
                list(accepted_lanes),
            ),
        )
        row = await cur.fetchone()
    return None if row is None else Job.model_validate(row)


async def count_claimable(
    conn: AsyncConnection, *, accepted_lanes: Sequence[str] = DEFAULT_DISPATCH_LANES
) -> int:
    """Return the number of jobs a ``dequeue`` could currently claim (the queue depth).

    Mirrors :func:`dequeue`'s eligibility predicate for ``accepted_lanes`` without locking or
    claiming — a read-only depth sample for the ``kdive.job.queue.depth`` gauge (ADR-0090 §5).
    It does not include jobs paused by ``queue_paused`` state because pausing is a separate
    operator gate.
    """
    if not accepted_lanes:
        return 0
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT public.count_claimable_worker_jobs(%s::text[])",
            (list(accepted_lanes),),
        )
        row = await cur.fetchone()
    return int(row[0]) if row is not None else 0


async def heartbeat(
    conn: AsyncConnection,
    job_id: UUID,
    *,
    attempt: int,
    incarnation_credential: SecretStr,
    lease: timedelta = DEFAULT_LEASE,
) -> bool:
    """Renew ``job_id`` when the credential owns its exact running attempt.

    ``lease`` is applied to the PostgreSQL ``clock_timestamp()`` captured after the blocking
    incarnation fence and exact running-attempt row lock. Its computed deadline must be after that
    post-lock reference and no more than one hour later. This is a per-heartbeat limit, not a total
    job runtime limit. SQLSTATE ``22023`` leaves the row unchanged; retry with an interval whose
    computed deadline is valid.

    Returns:
        ``True`` when a row matched; ``False`` when the job is no longer this worker's
        running job (reclaimed, completed, failed, or canceled).
    """
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            "SELECT public.heartbeat_worker_job(%s, sha256(convert_to(%s, 'UTF8')), %s, %s)",
            (job_id, incarnation_credential.get_secret_value(), attempt, lease),
        )
        row = await cur.fetchone()
    return row == (True,)


async def complete(
    conn: AsyncConnection,
    job_id: UUID,
    result_ref: str | None,
    *,
    attempt: int,
    incarnation_credential: SecretStr,
) -> Job | None:
    """Complete ``job_id`` when the credential owns its exact running attempt.

    Returns:
        The updated :class:`Job`, or ``None`` if the fence did not match (the worker
        lost the job to a reclaim; the caller logs and drops the result).
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM public.complete_worker_job(%s, sha256(convert_to(%s, 'UTF8')), %s, %s)",
            (
                job_id,
                incarnation_credential.get_secret_value(),
                attempt,
                result_ref,
            ),
        )
        row = await cur.fetchone()
    return None if row is None else Job.model_validate(row)


async def fail(
    conn: AsyncConnection,
    job: Job,
    error_category: ErrorCategory,
    *,
    incarnation_credential: SecretStr,
    terminal: bool = False,
    failure_context: Mapping[str, str] | None = None,
) -> Job:
    """Dead-letter or requeue ``job`` through its credential and exact attempt fence.

    Dead-letters (``running → failed`` with ``error_category``) when ``terminal`` is
    set (a non-retryable failure, e.g. no handler for the kind) or the already-charged
    ``job.attempt`` has reached ``job.max_attempts``; otherwise requeues
    (``running → queued``, clearing the lease) for another attempt.

    The worker calls this from inside a transaction that also carries the owning Run's terminal
    transition (ADR-0500), so the ``conn.transaction()`` below nests as a SAVEPOINT there and the
    two writes commit together. The fence is what keeps that safe: a reclaimed job's stale worker
    gets no row back, so the caller sees the job still ``running`` and leaves the Run alone.

    Returns:
        The job's post-write state, or the unchanged ``job`` when the fence missed
        (another worker reclaimed it).
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM public.fail_worker_job("
            "%s, sha256(convert_to(%s, 'UTF8')), %s, %s, %s, %s)",
            (
                job.id,
                incarnation_credential.get_secret_value(),
                job.attempt,
                error_category,
                Jsonb(dict(failure_context or {})),
                terminal,
            ),
        )
        row = await cur.fetchone()
    return job if row is None else Job.model_validate(row)


async def is_queue_paused(conn: AsyncConnection) -> bool:
    """Return the worker's ``queue_paused`` flag from the single-row ``ops_control``.

    Read before each ``dequeue`` (``Worker.run_once``): while paused the worker claims
    no new job but keeps heart-beating any job already in flight. ``ops_control`` is
    seeded with one row at migration time, so the read always finds it; a missing row is
    an unexpected schema state and **fails closed** (treated as paused) rather than
    silently claiming while the control row is absent.
    """
    async with conn.cursor() as cur:
        await cur.execute("SELECT queue_paused FROM ops_control WHERE singleton = true")
        row = await cur.fetchone()
    return True if row is None else bool(row[0])


async def set_queue_paused(conn: AsyncConnection, paused: bool) -> None:
    """Set the worker's ``queue_paused`` flag on the single-row ``ops_control``.

    ``ops.set_queue_paused`` calls this. Wraps the ``UPDATE`` in
    ``conn.transaction()`` so it self-commits on any connection.
    """
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            "UPDATE ops_control SET queue_paused = %s WHERE singleton = true",
            (paused,),
        )


async def all_recent_jobs(
    conn: AsyncConnection,
    limit: int,
    *,
    states: Sequence[JobState] | None = None,
    before: tuple[datetime, UUID] | None = None,
) -> list[Job]:
    """Return the most recent jobs across **every** project, newest first, capped.

    The platform view (``ops.jobs_list``, ADR-0062): unlike :func:`recent_jobs` this is
    **not** project-scoped — it spans all tenants for an operator's cross-project queue
    inspection, so its only caller must already hold ``platform_operator``. ``states``,
    when given, filters to those job states (e.g. ``[JobState.QUEUED]``); an empty
    sequence yields no rows. ``before`` is an exclusive ``(created_at, id)`` keyset
    boundary. The ``id`` tiebreaker totals the order on a shared ``created_at`` so pages
    neither skip nor repeat a tied row.
    """
    predicates: list[sql.Composable] = []
    params: dict[str, object] = {"limit": limit}
    if states is not None:
        predicates.append(sql.SQL("state = ANY(%(states)s::text[])"))
        params["states"] = [state.value for state in states]
    if before is not None:
        predicates.append(sql.SQL("(created_at, id) < (%(before_ts)s, %(before_id)s)"))
        params["before_ts"], params["before_id"] = before
    where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(predicates) if predicates else sql.SQL("")
    query = (
        sql.SQL("SELECT * FROM jobs")
        + where
        + sql.SQL(" ORDER BY created_at DESC, id DESC LIMIT %(limit)s")
    )
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        rows = await cur.fetchall()
    return [Job.model_validate(row) for row in rows]


async def latest_succeeded_job_for_system(
    conn: AsyncConnection, kind: JobKind, system_id: UUID
) -> Job | None:
    """Return the most recent ``succeeded`` ``kind`` job for ``system_id``, or ``None``.

    Matches on the job payload's ``system_id`` (the system-scoped kinds carry it) and
    ``state = succeeded`` so only a job that actually ran — and therefore carries a
    ``result_ref`` verdict — is returned. Newest first by ``(created_at, id)``. Used by the
    ``runs.get`` liveness read to fold in the latest ``check_ssh_reachable`` verdict (ADR-0373).
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM jobs WHERE kind = %s AND payload->>'system_id' = %s "
            "AND state = %s ORDER BY created_at DESC, id DESC LIMIT 1",
            (kind.value, str(system_id), JobState.SUCCEEDED.value),
        )
        row = await cur.fetchone()
    return Job.model_validate(row) if row is not None else None


_TERMINAL_JOB_STATES: frozenset[JobState] = frozenset(
    {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELED}
)
# Terminal states considered when attributing a System lifecycle outcome.


async def latest_failed_job_for_system(conn: AsyncConnection, system_id: UUID) -> Job | None:
    """Return the newest failed attributable lifecycle job, else ``None`` (ADR-0454).

    Only System-failing kinds qualify. The newest terminal row by ``(created_at, id)`` wins, so a
    newer success or cancellation masks an older failure rather than attributing it.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM jobs WHERE kind = ANY(%s::text[]) AND payload->>'system_id' = %s "
            "AND state = ANY(%s::text[]) ORDER BY created_at DESC, id DESC LIMIT 1",
            (
                sorted(kind.value for kind in SYSTEM_FAILING_JOB_KINDS),
                str(system_id),
                sorted(state.value for state in _TERMINAL_JOB_STATES),
            ),
        )
        row = await cur.fetchone()
        if row is not None and row["state"] != JobState.FAILED.value:
            return None
    return Job.model_validate(row) if row is not None else None


async def queue_depth(conn: AsyncConnection) -> dict[str, int]:
    """Return the cross-project job count per state (the platform queue depth).

    Spans every project (the platform view); states with no jobs are omitted. Used by
    ``ops.jobs_list`` to report queue depth alongside the per-job rows.
    """
    async with conn.cursor() as cur:
        await cur.execute("SELECT state, count(*) FROM jobs GROUP BY state")
        rows = await cur.fetchall()
    return {str(state): int(count) for state, count in rows}


async def recent_jobs(
    conn: AsyncConnection,
    limit: int,
    projects: Sequence[str],
    *,
    after: tuple[datetime, UUID] | None = None,
    status: JobState | None = None,
    kind: JobKind | None = None,
    investigation_id: UUID | None = None,
    system_id: UUID | None = None,
) -> list[Job]:
    """Return the caller's most recent jobs, newest first, capped at ``limit``.

    Scoped to ``projects``: only jobs whose ``authorizing->>'project'`` is one of the
    caller's granted projects are returned. An empty ``projects`` yields no rows,
    and a job whose ``authorizing`` carries no ``project`` belongs to no one (fail
    closed). The cap applies after the project filter, so the caller gets up to ``limit``
    of *their* jobs. The ``id`` tiebreaker makes the order total when two jobs share a
    ``created_at`` microsecond, so the cap never drops an arbitrary one of a tied pair.

    ``after`` is the ``(created_at, id)`` keyset boundary from a prior page's cursor
    (ADR-0192); when set, only rows strictly older than it (in ``created_at DESC, id DESC``
    order) are returned. The caller fetches ``limit`` already incremented by one so it can
    detect truncation.

    Optional filters (ADR-0197), applied before the keyset seek so the cursor stays a pure
    boundary across pages:

    - ``status`` / ``kind`` are equality predicates on the ``state`` / ``kind`` columns.
    - ``investigation_id`` filters to jobs whose Run belongs to that Investigation. The
      ``jobs`` table has no Run/Investigation column, so the query joins ``runs`` on
      ``jobs.payload->>'run_id'``; only run-bearing kinds (``build``/``install``/``boot``)
      carry a ``run_id``, so non-run-bearing jobs never match. The project predicate still
      gates every row, so an Investigation in an unreadable project yields no rows.
    - ``system_id`` filters to the system-scoped jobs carrying that System in their payload
      (``authorize_ssh_key``/``check_ssh_reachable``/provision/…), an equality predicate on
      ``j.payload->>'system_id'`` — the same key ``latest_succeeded_job_for_system`` matches.
      These jobs carry no ``run_id``, so ``investigation_id`` never reaches them; ``system_id``
      is how an agent lists them (ADR-0376).
    """
    # Qualify every job column so the optional `runs` join cannot make `created_at`/`id`
    # ambiguous, and so `j.*` returns only `jobs` columns (a bare `*` would pull `runs`
    # columns into the row and break `Job.model_validate`). Composed via psycopg.sql so the
    # statically-built fragments stay type-safe and the values bind as parameters.
    join = sql.SQL("")
    clauses = [sql.SQL("j.authorizing->>'project' = ANY(%s::text[])")]
    params: list[object] = [list(projects)]
    if investigation_id is not None:
        join = sql.SQL(" JOIN runs r ON r.id::text = j.payload->>'run_id'")
        clauses.append(sql.SQL("r.investigation_id = %s"))
        params.append(investigation_id)
    if status is not None:
        clauses.append(sql.SQL("j.state = %s"))
        params.append(status.value)
    if kind is not None:
        clauses.append(sql.SQL("j.kind = %s"))
        params.append(kind.value)
    if system_id is not None:
        clauses.append(sql.SQL("j.payload->>'system_id' = %s"))
        params.append(str(system_id))
    if after is not None:
        clauses.append(sql.SQL("(j.created_at, j.id) < (%s, %s)"))
        params.extend(after)
    params.append(limit)
    query = sql.SQL(
        "SELECT j.* FROM jobs j{join} WHERE {where} ORDER BY j.created_at DESC, j.id DESC LIMIT %s"
    ).format(join=join, where=sql.SQL(" AND ").join(clauses))
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        rows = await cur.fetchall()
    return [Job.model_validate(row) for row in rows]
