"""A `failed` System reports its failing job's real category, not a hard-coded one (#1550).

`systems.get` used to render every `SystemState.FAILED` as `infrastructure_failure`, which
`RETRYABLE_BY_CATEGORY` turns into `retryable: true` — telling an agent to retry a
non-retryable configuration error. ADR-0454 resolves the category from the job that actually
failed the System, keeping the default only for the path with no attributable job.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import SYSTEMS
from kdive.domain.capacity.state import JobState, SystemState
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle.records import System
from kdive.domain.operations.jobs import SYSTEM_FAILING_JOB_KINDS, Job, JobKind
from kdive.jobs import queue
from kdive.jobs.payloads import (
    CheckSshReachablePayload,
    ReprovisionPayload,
    RestorePayload,
    SystemPayload,
)
from kdive.mcp.exposure import _REGISTERED_TOOLS
from kdive.mcp.tools.lifecycle.systems.view import (
    ABANDONED_JOB_SYSTEM_FAILURE_DETAIL,
    FAILED_SYSTEM_NEXT_ACTIONS,
    FAILED_SYSTEM_UNCITED_NEXT_ACTIONS,
    NO_JOB_SYSTEM_FAILURE_DETAIL,
    RECORDED_SYSTEM_FAILURE_DETAIL,
    UNREPORTABLE_JOB_SYSTEM_FAILURE_DETAIL,
    SystemsListRequest,
    get_system,
    list_systems,
    system_envelope,
)
from kdive.reconciler.repairs.jobs import repair_abandoned_jobs
from kdive.reconciler.repairs.systems import repair_stalled_restoring_systems
from tests.mcp.systems_support import (
    ctx as _ctx,
)
from tests.mcp.systems_support import (
    granted_allocation as _granted_allocation,
)
from tests.mcp.systems_support import (
    pool as _pool,
)
from tests.mcp.systems_support import (
    provider_resolver as _provider_resolver,
)
from tests.mcp.systems_support import (
    provisioning_profile as _provisioning_profile,
)

_DT = datetime(2026, 1, 1, tzinfo=UTC)


# --- unit: the envelope ---------------------------------------------------------------


def _system(
    state: SystemState = SystemState.FAILED,
    *,
    failure_category: ErrorCategory | None = None,
) -> System:
    return System(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="user-1",
        project="proj",
        allocation_id=uuid4(),
        state=state,
        provisioning_profile=_provisioning_profile(),
        failure_category=failure_category,
    )


def _failed_job(
    category: ErrorCategory | None,
    failure_context: dict[str, str] | None = None,
    *,
    kind: JobKind = JobKind.PROVISION,
) -> Job:
    return Job(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        kind=kind,
        payload={"system_id": str(uuid4())},
        state=JobState.FAILED,
        max_attempts=3,
        error_category=category,
        failure_context=failure_context or {},
        authorizing={"principal": "user-1", "agent_session": "s", "project": "proj"},
        dedup_key="dk-1",
    )


def test_failed_system_reports_the_jobs_category_not_infrastructure_failure() -> None:
    # The #1550 regression: a configuration error must not be reported as a retryable
    # infrastructure fault just because the System is `failed`.
    message = (
        "uploaded rootfs checksum is not owned by this System's investigation; "
        "finalize the upload in the investigation this System is bound to"
    )
    job = _failed_job(ErrorCategory.CONFIGURATION_ERROR, {"failure_message": message})

    resp = system_envelope(_system(), failing_job=job, failure_attributed=True)

    assert resp.status == "error"
    assert resp.error_category == "configuration_error"
    assert resp.retryable is False
    assert resp.detail == message
    assert resp.data["failing_job_id"] == str(job.id)
    assert resp.data["current_status"] == "failed"


def test_failed_system_without_a_job_keeps_the_infrastructure_default() -> None:
    # A `failed` System with neither a recorded category nor an attributable job: a row that
    # predates migration 0083, or a non-CategorizedError escape that dead-lettered the job
    # without reaching `_record_system_failure`. The default is what those paths are for.
    # `repair_stalled_restoring_systems` used to be a third; ADR-0513 gave it a real verdict.
    resp = system_envelope(_system(), failing_job=None, failure_attributed=True)

    assert resp.error_category == "infrastructure_failure"
    assert resp.retryable is True
    assert resp.detail == NO_JOB_SYSTEM_FAILURE_DETAIL
    assert resp.detail
    assert "failing_job_id" not in resp.data


def test_failed_system_with_uncategorized_job_falls_back_to_the_default() -> None:
    # A job row whose `error_category` is NULL (never dead-lettered) answers nothing.
    job = _failed_job(None, {"failure_message": "half-written"})

    resp = system_envelope(_system(), failing_job=job, failure_attributed=True)

    assert resp.error_category == "infrastructure_failure"
    assert resp.data["failing_job_id"] == str(job.id)


def test_failed_system_job_without_message_still_gets_a_reason() -> None:
    # A job with an empty `failure_context` is as reasonless as no job, so the fallback keys on
    # the resolved detail rather than on the job's presence — a bare category is the surface
    # #1550 exists to remove.
    job = _failed_job(ErrorCategory.PROVISIONING_FAILURE, {})

    resp = system_envelope(_system(), failing_job=job, failure_attributed=True)

    assert resp.error_category == "provisioning_failure"
    assert resp.detail == NO_JOB_SYSTEM_FAILURE_DETAIL
    assert resp.data["failing_job_id"] == str(job.id)


def test_failed_system_attributed_to_an_abandoned_job_says_so() -> None:
    # `repair_abandoned_jobs` dead-letters a zombie job with `lease_expired` and an untouched
    # (empty) `failure_context`, and the System-failing reconciler repairs run after it — so
    # this, not "no job at all", is what a worker that died actually leaves behind.
    job = _failed_job(ErrorCategory.LEASE_EXPIRED, {}, kind=JobKind.RESTORE)

    resp = system_envelope(_system(), failing_job=job, failure_attributed=True)

    assert resp.error_category == "lease_expired"
    assert resp.detail == ABANDONED_JOB_SYSTEM_FAILURE_DETAIL
    assert resp.detail != NO_JOB_SYSTEM_FAILURE_DETAIL
    assert resp.data["failing_job_id"] == str(job.id)


def test_recorded_category_outranks_a_lease_expired_job() -> None:
    # ADR-0492 §3: the System's own column is written in the transaction that commits the
    # `failed` transition, so it survives a worker that died before `queue.fail` — the exact
    # window in which the reconciler stamps `lease_expired` over the job.
    job = _failed_job(ErrorCategory.LEASE_EXPIRED, {})

    resp = system_envelope(
        _system(failure_category=ErrorCategory.CONFIGURATION_ERROR),
        failing_job=job,
        failure_attributed=True,
    )

    assert resp.error_category == "configuration_error"
    assert resp.retryable is False
    # The reason must not be either job-shaped string: this envelope *is* reporting a retained
    # verdict, so "the original failure reason was not retained" and "no job recorded a reason"
    # are both false beside it. Only the redacted message, which lives on the job, was lost.
    assert resp.detail == RECORDED_SYSTEM_FAILURE_DETAIL
    assert resp.detail != ABANDONED_JOB_SYSTEM_FAILURE_DETAIL
    assert resp.detail != NO_JOB_SYSTEM_FAILURE_DETAIL
    assert resp.data["failing_job_id"] == str(job.id)


def test_recorded_category_answers_when_the_retry_ended_succeeded() -> None:
    # ADR-0492 Context shape 2: with attempts remaining the reclaimed job re-enters a terminal
    # System, early-returns, and ends `succeeded`, so `latest_failed_job_for_system` attributes
    # nothing — `failing_job=None`. The System's own record is the only thing left.
    resp = system_envelope(
        _system(failure_category=ErrorCategory.CONFIGURATION_ERROR),
        failing_job=None,
        failure_attributed=True,
    )

    assert resp.error_category == "configuration_error"
    assert resp.retryable is False
    assert resp.detail == RECORDED_SYSTEM_FAILURE_DETAIL
    assert "failing_job_id" not in resp.data


def test_recorded_no_leak_category_falls_through_to_the_job() -> None:
    # ADR-0492 §3: the no-leak rule is a property of the category, not of where it was written,
    # so a recorded `not_found` is not reported. But *only* the category is dropped — the job
    # still explains itself, and discarding its category, message, and id would buy nothing.
    job = _failed_job(ErrorCategory.CONFIGURATION_ERROR, {"failure_message": "volume not staged"})

    resp = system_envelope(
        _system(failure_category=ErrorCategory.NOT_FOUND),
        failing_job=job,
        failure_attributed=True,
    )

    assert resp.error_category == "configuration_error"
    assert resp.detail == "volume not staged"
    assert resp.data["failing_job_id"] == str(job.id)


def test_recorded_no_leak_category_with_no_job_takes_the_default() -> None:
    # Nothing to fall through to: the category is dropped and the default is all that is left.
    resp = system_envelope(
        _system(failure_category=ErrorCategory.AUTHORIZATION_DENIED),
        failing_job=None,
        failure_attributed=True,
    )

    assert resp.error_category == "infrastructure_failure"
    assert resp.detail == NO_JOB_SYSTEM_FAILURE_DETAIL
    assert "failing_job_id" not in resp.data


def test_recorded_category_survives_dropping_a_no_leak_job() -> None:
    # The converse is deliberately asymmetric: the *job* is dropped whole for its no-leak
    # category, but the System's own recorded verdict is a different fact from a write this
    # surface trusts, so it is still reported.
    job = _failed_job(ErrorCategory.NOT_FOUND, {"failure_message": "secret-host-name"})

    resp = system_envelope(
        _system(failure_category=ErrorCategory.PROVISIONING_FAILURE),
        failing_job=job,
        failure_attributed=True,
    )

    assert resp.error_category == "provisioning_failure"
    assert resp.detail == UNREPORTABLE_JOB_SYSTEM_FAILURE_DETAIL
    assert "failing_job_id" not in resp.data
    assert "secret-host-name" not in str(resp.model_dump())


def test_unattributed_system_reports_its_recorded_category_but_no_reason() -> None:
    # ADR-0492 Consequences: the column is on the row the list path already reads, so the
    # category needs no query — but `detail` stays gated on having *run* the lookup, since
    # every derived reason is a positive claim about a fact that was checked.
    resp = system_envelope(_system(failure_category=ErrorCategory.CONFIGURATION_ERROR))

    assert resp.error_category == "configuration_error"
    assert resp.retryable is False
    assert resp.detail is None
    assert "failing_job_id" not in resp.data


def test_failed_system_offers_the_recovery_actions_not_a_dead_end() -> None:
    # `SystemState.FAILED` is terminal, so `retryable` names no System-scoped tool to retry
    # with; the way forward is the one `systems.provision` already names (ADR-0149).
    resp = system_envelope(
        _system(),
        failing_job=_failed_job(ErrorCategory.CONFIGURATION_ERROR),
        failure_attributed=True,
    )

    assert resp.suggested_next_actions == [
        "jobs.wait",
        "allocations.release",
        "allocations.request",
    ]


def test_failed_system_next_actions_name_registered_tools() -> None:
    # `visible_next_actions` raises on an unregistered breadcrumb (#1444); this envelope builds
    # its list unfiltered, so the registry check has to happen here.
    every_action = {*FAILED_SYSTEM_NEXT_ACTIONS, *FAILED_SYSTEM_UNCITED_NEXT_ACTIONS}
    assert all(name in _REGISTERED_TOOLS for name in every_action)


def test_failed_system_does_not_forward_a_no_leak_category_as_its_own_verdict() -> None:
    # `not_found` is a reserved envelope-level meaning: `systems.get` returns it for a System
    # that is absent or invisible. Echoing a job's `not_found` onto a System the caller just
    # read would say "this object is gone" in an envelope carrying that object's whole record.
    # Reachable: a `restore` binding a System whose Resource row vanished dead-letters
    # `not_found` without touching System state, and the reconciler then fails the System.
    job = _failed_job(
        ErrorCategory.NOT_FOUND,
        {"failure_message": "secret-host-name leaked here"},
    )

    resp = system_envelope(_system(), failing_job=job, failure_attributed=True)

    assert resp.error_category == "infrastructure_failure"
    # Not the "no job recorded a reason" string: a job did record one, it is simply not
    # repeatable here — and the envelope points at where it survives.
    assert resp.detail == UNREPORTABLE_JOB_SYSTEM_FAILURE_DETAIL
    assert resp.suggested_next_actions[0] == "jobs.list"
    assert "failing_job_id" not in resp.data
    assert "secret-host-name" not in str(resp.model_dump())


def test_failed_system_does_not_forward_authorization_denied_either() -> None:
    # The other no-leak category, for the same reason.
    job = _failed_job(ErrorCategory.AUTHORIZATION_DENIED, {"failure_message": "secret-project"})

    resp = system_envelope(_system(), failing_job=job, failure_attributed=True)

    assert resp.error_category == "infrastructure_failure"
    assert resp.detail == UNREPORTABLE_JOB_SYSTEM_FAILURE_DETAIL
    assert "failing_job_id" not in resp.data
    assert "secret-project" not in str(resp.model_dump())


def test_unattributed_failed_system_states_nothing_it_did_not_check() -> None:
    # The list path never resolves a failing job. `failing_job=None` alone cannot mean "looked
    # and found none" there, so no derived reason may be asserted — silence, as before #1550.
    # Claiming "no job recorded a reason" would be a confident falsehood *and* would give an
    # agent a reason not to call the two tools where the reason actually lives.
    resp = system_envelope(_system(), resource_kind="local-libvirt", resource_id="res-1")

    assert resp.error_category == "infrastructure_failure"
    assert resp.detail is None
    assert "failing_job_id" not in resp.data


def test_non_failed_system_ignores_a_supplied_job() -> None:
    job = _failed_job(ErrorCategory.CONFIGURATION_ERROR, {"failure_message": "irrelevant"})

    resp = system_envelope(_system(SystemState.READY), failing_job=job)

    assert resp.status == "ready"
    assert resp.error_category is None
    assert "failing_job_id" not in resp.data


def test_system_failing_job_kinds_are_the_kinds_that_write_failed() -> None:
    # ADR-0454 §2: exactly the kinds whose handlers write `SystemState.FAILED`. Widening this
    # set without a matching writer would let an unrelated failure be reported as the reason.
    assert set(SYSTEM_FAILING_JOB_KINDS) == {
        JobKind.PROVISION,
        JobKind.REPROVISION,
        JobKind.RESTORE,
    }


# --- integration: the lookup and the read path ----------------------------------------


async def _seed_system(
    conn_pool: AsyncConnectionPool,
    alloc_id: str,
    state: SystemState,
    *,
    failure_category: ErrorCategory | None = None,
) -> UUID:
    async with conn_pool.connection() as conn:
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                allocation_id=UUID(alloc_id),
                state=state,
                provisioning_profile=_provisioning_profile(),
                failure_category=failure_category,
            ),
        )
    return system.id


def _payload_for(kind: JobKind, system_id: UUID) -> SystemPayload:
    """The payload model ``kind`` validates against, carrying ``system_id`` as its join key."""
    if kind is JobKind.REPROVISION:
        return ReprovisionPayload(system_id=str(system_id), profile_digest="digest-1")
    if kind is JobKind.CHECK_SSH_REACHABLE:
        return CheckSshReachablePayload(system_id=str(system_id))
    if kind is JobKind.RESTORE:
        return RestorePayload(system_id=str(system_id), name="snap-1", start_paused=False)
    return SystemPayload(system_id=str(system_id))


async def _dead_letter(
    conn_pool: AsyncConnectionPool,
    *,
    kind: JobKind,
    system_id: UUID,
    dedup_key: str,
    category: ErrorCategory,
    message: str = "boom",
) -> Job:
    """Enqueue, claim, and dead-letter a job the way the worker does."""
    payload = _payload_for(kind, system_id)
    async with conn_pool.connection() as conn:
        await queue.enqueue(
            conn,
            kind,
            payload,
            {"principal": "user-1", "agent_session": "s", "project": "proj"},
            dedup_key,
        )
        claimed = await queue.dequeue(conn, "w1")
        assert claimed is not None
        return await queue.fail(
            conn,
            claimed,
            category,
            terminal=True,
            failure_context={"failure_message": message},
        )


def test_latest_failed_job_for_system_finds_the_dead_lettered_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as conn_pool:
            alloc_id = await _granted_allocation(conn_pool)
            system_id = await _seed_system(conn_pool, alloc_id, SystemState.FAILED)
            job = await _dead_letter(
                conn_pool,
                kind=JobKind.PROVISION,
                system_id=system_id,
                dedup_key="dk-provision",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
            async with conn_pool.connection() as conn:
                found = await queue.latest_failed_job_for_system(conn, system_id)
        assert found is not None
        assert found.id == job.id
        assert found.error_category is ErrorCategory.CONFIGURATION_ERROR

    asyncio.run(_run())


def test_latest_failed_job_for_system_ignores_an_unrelated_kind(migrated_url: str) -> None:
    # A failed `check_ssh_reachable` is routine and never wrote `SystemState.FAILED`; reading
    # it as the reason a System is `failed` would be a confident mis-attribution.
    async def _run() -> None:
        async with _pool(migrated_url) as conn_pool:
            alloc_id = await _granted_allocation(conn_pool)
            system_id = await _seed_system(conn_pool, alloc_id, SystemState.FAILED)
            await _dead_letter(
                conn_pool,
                kind=JobKind.CHECK_SSH_REACHABLE,
                system_id=system_id,
                dedup_key="dk-ssh",
                category=ErrorCategory.TRANSPORT_FAILURE,
            )
            async with conn_pool.connection() as conn:
                found = await queue.latest_failed_job_for_system(conn, system_id)
        assert found is None

    asyncio.run(_run())


def test_latest_failed_job_for_system_ignores_another_systems_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as conn_pool:
            alloc_id = await _granted_allocation(conn_pool)
            mine = await _seed_system(conn_pool, alloc_id, SystemState.FAILED)
            theirs = await _seed_system(conn_pool, alloc_id, SystemState.FAILED)
            await _dead_letter(
                conn_pool,
                kind=JobKind.PROVISION,
                system_id=theirs,
                dedup_key="dk-theirs",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
            async with conn_pool.connection() as conn:
                found = await queue.latest_failed_job_for_system(conn, mine)
        assert found is None

    asyncio.run(_run())


def test_latest_failed_job_for_system_ignores_a_non_failed_job(migrated_url: str) -> None:
    # A queued/running job carries no `error_category` — `queue.fail` writes it only on the
    # dead-letter branch — so matching a non-`failed` row would answer with NULL.
    async def _run() -> None:
        async with _pool(migrated_url) as conn_pool:
            alloc_id = await _granted_allocation(conn_pool)
            system_id = await _seed_system(conn_pool, alloc_id, SystemState.FAILED)
            async with conn_pool.connection() as conn:
                await queue.enqueue(
                    conn,
                    JobKind.PROVISION,
                    SystemPayload(system_id=str(system_id)),
                    {"principal": "user-1", "agent_session": "s", "project": "proj"},
                    "dk-queued",
                )
                found = await queue.latest_failed_job_for_system(conn, system_id)
        assert found is None

    asyncio.run(_run())


def test_latest_failed_job_for_system_does_not_skip_over_a_newer_canceled_job(
    migrated_url: str,
) -> None:
    # A canceled `restore` satisfies `repair_stalled_restoring_systems`' "no restore job
    # queued/running" predicate, so the System reaches `failed` with nothing to attribute. If
    # the query filtered to `failed` and took the newest match, it would skip over the cancel to
    # a stale earlier provision failure and report it as authoritative — worse than the default,
    # because it looks certain.
    async def _run() -> None:
        async with _pool(migrated_url) as conn_pool:
            alloc_id = await _granted_allocation(conn_pool)
            system_id = await _seed_system(conn_pool, alloc_id, SystemState.FAILED)
            await _dead_letter(
                conn_pool,
                kind=JobKind.PROVISION,
                system_id=system_id,
                dedup_key="dk-stale",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
            async with conn_pool.connection() as conn:
                job = await queue.enqueue(
                    conn,
                    JobKind.RESTORE,
                    _payload_for(JobKind.RESTORE, system_id),
                    {"principal": "user-1", "agent_session": "s", "project": "proj"},
                    "dk-restore",
                )
                await conn.execute("UPDATE jobs SET state = 'canceled' WHERE id = %s", (job.id,))
                found = await queue.latest_failed_job_for_system(conn, system_id)
        assert found is None

    asyncio.run(_run())


def test_latest_failed_job_for_system_does_not_skip_over_a_newer_success(
    migrated_url: str,
) -> None:
    # Same rule the other way: if the last system-lifecycle job to finish succeeded, an older
    # failure is not the explanation for anything.
    async def _run() -> None:
        async with _pool(migrated_url) as conn_pool:
            alloc_id = await _granted_allocation(conn_pool)
            system_id = await _seed_system(conn_pool, alloc_id, SystemState.FAILED)
            await _dead_letter(
                conn_pool,
                kind=JobKind.PROVISION,
                system_id=system_id,
                dedup_key="dk-stale",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
            async with conn_pool.connection() as conn:
                await queue.enqueue(
                    conn,
                    JobKind.REPROVISION,
                    _payload_for(JobKind.REPROVISION, system_id),
                    {"principal": "user-1", "agent_session": "s", "project": "proj"},
                    "dk-reprovision",
                )
                claimed = await queue.dequeue(conn, "w1")
                assert claimed is not None
                assert await queue.complete(conn, claimed.id, "w1", None) is not None
                found = await queue.latest_failed_job_for_system(conn, system_id)
        assert found is None

    asyncio.run(_run())


def test_latest_failed_job_for_system_takes_the_newest(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as conn_pool:
            alloc_id = await _granted_allocation(conn_pool)
            system_id = await _seed_system(conn_pool, alloc_id, SystemState.FAILED)
            await _dead_letter(
                conn_pool,
                kind=JobKind.PROVISION,
                system_id=system_id,
                dedup_key="dk-first",
                category=ErrorCategory.PROVISIONING_FAILURE,
            )
            newest = await _dead_letter(
                conn_pool,
                kind=JobKind.REPROVISION,
                system_id=system_id,
                dedup_key="dk-second",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
            async with conn_pool.connection() as conn:
                found = await queue.latest_failed_job_for_system(conn, system_id)
        assert found is not None
        assert found.id == newest.id

    asyncio.run(_run())


def test_get_system_surfaces_the_provision_jobs_category(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as conn_pool:
            alloc_id = await _granted_allocation(conn_pool)
            system_id = await _seed_system(conn_pool, alloc_id, SystemState.FAILED)
            job = await _dead_letter(
                conn_pool,
                kind=JobKind.PROVISION,
                system_id=system_id,
                dedup_key="dk-provision",
                category=ErrorCategory.CONFIGURATION_ERROR,
                message="rootfs checksum is not owned by this System's investigation",
            )
            resp = await get_system(
                conn_pool, _ctx(), str(system_id), resolver=_provider_resolver()
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.retryable is False
        assert resp.detail == "rootfs checksum is not owned by this System's investigation"
        assert resp.data["failing_job_id"] == str(job.id)

    asyncio.run(_run())


def test_get_system_after_the_real_abandoned_job_sweep(migrated_url: str) -> None:
    # The production route into a "reconciler orphan": `repair_abandoned_jobs` dead-letters the
    # zombie restore job (`lease_expired`, `failure_context` untouched), and only then does
    # `repair_stalled_restoring_systems` resolve the System to `failed`. Driven through the real
    # reconciler rather than a hand-built row, so the shape cannot drift from the sweep.
    #
    # ADR-0513 re-baselines what this reports. The repair now stamps `restore_incomplete`, and
    # the System's own verdict outranks the job's (ADR-0492 §3), so the envelope stops quoting
    # `lease_expired` — a true statement about the *job's* bookkeeping that read as a retryable
    # verdict on the System. The job is still cited; only the category it lends is superseded.
    async def _run() -> None:
        async with _pool(migrated_url) as conn_pool:
            alloc_id = await _granted_allocation(conn_pool)
            system_id = await _seed_system(conn_pool, alloc_id, SystemState.RESTORING)
            async with conn_pool.connection() as conn:
                await queue.enqueue(
                    conn,
                    JobKind.RESTORE,
                    _payload_for(JobKind.RESTORE, system_id),
                    {"principal": "user-1", "agent_session": "s", "project": "proj"},
                    "dk-restore",
                )
                # A zombie: claimed, out of attempts, lease long lapsed.
                await conn.execute(
                    "UPDATE jobs SET state = 'running', worker_id = 'w1', attempt = 3, "
                    "max_attempts = 3, lease_expires_at = now() - interval '1 hour'"
                )
                assert await repair_abandoned_jobs(conn) == 1
                assert await repair_stalled_restoring_systems(conn) == 1
            resp = await get_system(
                conn_pool, _ctx(), str(system_id), resolver=_provider_resolver()
            )
        assert resp.status == "error"
        assert resp.error_category == "restore_incomplete"
        assert resp.retryable is False  # `lease_expired` was already false; the reason changed
        assert resp.detail == RECORDED_SYSTEM_FAILURE_DETAIL
        assert resp.detail != ABANDONED_JOB_SYSTEM_FAILURE_DETAIL
        assert "failing_job_id" in resp.data

    asyncio.run(_run())


def test_get_system_falls_back_when_no_job_failed_it(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as conn_pool:
            alloc_id = await _granted_allocation(conn_pool)
            system_id = await _seed_system(conn_pool, alloc_id, SystemState.FAILED)
            resp = await get_system(
                conn_pool, _ctx(), str(system_id), resolver=_provider_resolver()
            )
        assert resp.error_category == "infrastructure_failure"
        assert resp.detail == NO_JOB_SYSTEM_FAILURE_DETAIL
        assert "failing_job_id" not in resp.data

    asyncio.run(_run())


def test_get_system_on_a_ready_system_makes_no_job_lookup(migrated_url: str) -> None:
    # The lookup is on the `failed` branch only; a dead-lettered provision left over from an
    # earlier attempt must not colour a System that is now `ready`.
    async def _run() -> None:
        async with _pool(migrated_url) as conn_pool:
            alloc_id = await _granted_allocation(conn_pool)
            system_id = await _seed_system(conn_pool, alloc_id, SystemState.READY)
            await _dead_letter(
                conn_pool,
                kind=JobKind.PROVISION,
                system_id=system_id,
                dedup_key="dk-provision",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
            resp = await get_system(
                conn_pool, _ctx(), str(system_id), resolver=_provider_resolver()
            )
        assert resp.status == "ready"
        assert resp.error_category is None

    asyncio.run(_run())


def test_systems_list_reports_a_recorded_category_without_a_query(migrated_url: str) -> None:
    # ADR-0492 Consequences: the column is on the row `systems.list` already reads, so the list
    # path reports the truthful category with no per-row lookup — narrowing the gap ADR-0454 §4
    # disclosed. `detail` stays silent there, because the list path still checks nothing.
    async def _run() -> None:
        async with _pool(migrated_url) as conn_pool:
            alloc_id = await _granted_allocation(conn_pool)
            system_id = await _seed_system(
                conn_pool,
                alloc_id,
                SystemState.FAILED,
                failure_category=ErrorCategory.CONFIGURATION_ERROR,
            )
            resp = await list_systems(
                conn_pool, _ctx(), SystemsListRequest(state=SystemState.FAILED.value)
            )
        assert [item.object_id for item in resp.items] == [str(system_id)]
        assert resp.items[0].error_category == "configuration_error"
        assert resp.items[0].retryable is False
        assert resp.items[0].detail is None

    asyncio.run(_run())


def test_systems_list_keeps_the_flattened_category(migrated_url: str) -> None:
    # ADR-0454 §4, pinned so the *remaining* gap is a fact rather than an assumption: a System
    # that recorded no category of its own still needs the per-row lookup the list path does not
    # do, so it still reports the default. ADR-0513 narrowed which Systems those are — the
    # job-less reconciler repair now records one, leaving pre-ADR-0492 rows and the
    # non-CategorizedError escape as the shapes this covers. It is seeded directly, because the
    # reconciler no longer produces it.
    async def _run() -> None:
        async with _pool(migrated_url) as conn_pool:
            alloc_id = await _granted_allocation(conn_pool)
            system_id = await _seed_system(conn_pool, alloc_id, SystemState.FAILED)
            await _dead_letter(
                conn_pool,
                kind=JobKind.PROVISION,
                system_id=system_id,
                dedup_key="dk-provision",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
            resp = await list_systems(
                conn_pool, _ctx(), SystemsListRequest(state=SystemState.FAILED.value)
            )
        assert [item.object_id for item in resp.items] == [str(system_id)]
        assert resp.items[0].error_category == "infrastructure_failure"
        # But it must not *claim* the reason does not exist — this System's job recorded an
        # excellent one. The flattened category is the disclosed gap; a confident falsehood
        # about it is not.
        assert resp.items[0].detail is None
        assert resp.items[0].detail != NO_JOB_SYSTEM_FAILURE_DETAIL

    asyncio.run(_run())


def test_systems_list_reports_restore_incomplete_after_the_reconciler_sweep(
    migrated_url: str,
) -> None:
    # #1560 end to end: the reconciler path was the one failure path whose reason no per-row job
    # lookup could ever have recovered — it leaves no failed job to attribute — so it listed as
    # the `infrastructure_failure` default and, worse, as `retryable: true`. Driven through the
    # real repair rather than a seeded column, so the two halves of the fix are proved together.
    async def _run() -> None:
        async with _pool(migrated_url) as conn_pool:
            alloc_id = await _granted_allocation(conn_pool)
            system_id = await _seed_system(conn_pool, alloc_id, SystemState.RESTORING)
            async with conn_pool.connection() as conn:
                await queue.enqueue(
                    conn,
                    JobKind.RESTORE,
                    _payload_for(JobKind.RESTORE, system_id),
                    {"principal": "user-1", "agent_session": "s", "project": "proj"},
                    "dk-restore",
                )
                # The worker died mid-revert: its job is dead-lettered and can never run again.
                await conn.execute("UPDATE jobs SET state = 'failed', worker_id = NULL")
                assert await repair_stalled_restoring_systems(conn) == 1
            resp = await list_systems(
                conn_pool, _ctx(), SystemsListRequest(state=SystemState.FAILED.value)
            )
        assert [item.object_id for item in resp.items] == [str(system_id)]
        assert resp.items[0].error_category == "restore_incomplete"
        assert resp.items[0].error_category != "infrastructure_failure"
        # The harm the default did was not only vagueness: it told the agent to retry a System
        # whose disk is indeterminate and which is fenced from every lifecycle op.
        assert resp.items[0].retryable is False
        # `detail` stays get-only on the list path (ADR-0492 Consequences) — unchanged here.
        assert resp.items[0].detail is None

    asyncio.run(_run())
