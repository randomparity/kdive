"""The provider-agnostic orphaned-capture sweep (ADR-0556, #1946).

Fixtures build the job payload through the real :class:`CaptureTrafficPayload` so a
payload-shape change reddens these tests rather than leaving a sweep that selects nothing.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.sql import SQL, Identifier, Literal
from psycopg.types.json import Jsonb

from kdive.domain.capacity.state import JobState
from kdive.jobs.payloads import CaptureTrafficPayload
from kdive.providers.infra.reaping import CaptureReaper, NullCaptureReaper, OrphanedCapture
from kdive.reconciler.cleanup.provider_reaping import (
    DEFAULT_CAPTURE_REAP_BATCH,
    DEFAULT_CAPTURE_RETRY_BASE,
    DEFAULT_CAPTURE_RETRY_CAP,
    reap_orphaned_captures,
)

_LOCAL = "local-libvirt"
_REMOTE = "remote-libvirt"
_SETTLE = timedelta(minutes=30)
_PAST_SETTLE = timedelta(minutes=45)
_ROLE_AUTHENTICATION = "capture-reap-role-test"


class _Reaper:
    """Records every dispatch and reports a scripted outcome per job."""

    def __init__(self, *, reclaims: bool = True, raises_for: frozenset[UUID] = frozenset()) -> None:
        self.reclaims = reclaims
        self.raises_for = raises_for
        self.seen: list[OrphanedCapture] = []

    async def reclaim_capture(self, capture: OrphanedCapture) -> bool:
        self.seen.append(capture)
        if capture.job_id in self.raises_for:
            raise RuntimeError("provider host unreachable")
        return self.reclaims


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


async def _one_id(cursor: psycopg.AsyncCursor) -> UUID:
    """The single id a seeding INSERT returned."""
    row = await cursor.fetchone()
    assert row is not None
    return row[0]


async def _seed_chain(
    conn: psycopg.AsyncConnection,
    *,
    kind: str = _REMOTE,
    nameless: bool = False,
    domain_name: str | None = None,
) -> tuple[UUID, UUID]:
    """Insert resource -> allocation -> system -> investigation -> run; return (system, run).

    Each chain gets its own Resource name because ``resources`` is unique on ``(kind, name)``.
    ``nameless=True`` leaves the column NULL, the ownership-integrity case ADR-0187 binding
    cannot resolve a host from.
    """
    resource_id = await _one_id(
        await conn.execute(
            "INSERT INTO resources (kind, name, pool, cost_class, status, host_uri) "
            "VALUES (%s, %s, 'p', 'c', 'available', 'qemu:///system') RETURNING id",
            (kind, None if nameless else f"host-{uuid4().hex[:12]}"),
        )
    )
    allocation_id = await _one_id(
        await conn.execute(
            "INSERT INTO allocations (principal, project, resource_id, state) "
            "VALUES ('alice', 'proj', %s, 'active') RETURNING id",
            (resource_id,),
        )
    )
    system_id = await _one_id(
        await conn.execute(
            "INSERT INTO systems (principal, project, allocation_id, state, "
            "    provisioning_profile, domain_name) "
            "VALUES ('alice', 'proj', %s, 'ready', %s, %s) RETURNING id",
            (allocation_id, Jsonb({"k": "v"}), domain_name),
        )
    )
    investigation_id = await _one_id(
        await conn.execute(
            "INSERT INTO investigations (principal, project, title, state) "
            "VALUES ('alice', 'proj', 't', 'open') RETURNING id"
        )
    )
    run_id = await _one_id(
        await conn.execute(
            "INSERT INTO runs (principal, project, investigation_id, system_id, target_kind, "
            "    state, build_profile) "
            "VALUES ('alice', 'proj', %s, %s, %s, 'running', %s) RETURNING id",
            (investigation_id, system_id, kind, Jsonb({"cfg": 1})),
        )
    )
    return system_id, run_id


async def _seed_capture_job(
    conn: psycopg.AsyncConnection,
    run_id: UUID,
    *,
    state: JobState = JobState.FAILED,
    updated_ago: timedelta = _PAST_SETTLE,
    before_cutoff: bool = True,
    attempt: int = 1,
) -> UUID:
    """Insert one ``capture_traffic`` job whose payload is the real validated model.

    Both timestamps are set in the INSERT because ``jobs_set_updated_at`` is a BEFORE UPDATE
    trigger: a later ``UPDATE ... SET updated_at`` is silently overwritten with the database
    clock, which is exactly why the sweep can treat ``jobs.updated_at`` as database-maintained.
    The migration stamps the cutover cutoff at install time, so a job is post-cutoff unless it is
    deliberately created behind that mark.
    """
    payload = CaptureTrafficPayload(run_id=str(run_id), duration_s=5, max_bytes=4096, snaplen=128)
    created = (
        "(SELECT cutoff_at - interval '1 minute' FROM capture_operation_cutoff WHERE singleton)"
        if before_cutoff
        else "now()"
    )
    cursor = await conn.execute(
        "INSERT INTO jobs (kind, payload, state, attempt, max_attempts, authorizing, dedup_key, "  # noqa: S608
        f"    created_at, updated_at) VALUES ('capture_traffic', %s, %s, %s, 5, %s, %s, {created}, "
        "    now() - %s) RETURNING id",
        (
            Jsonb(payload.model_dump(mode="json")),
            state.value,
            attempt,
            Jsonb({"principal": "p", "agent_session": None, "project": "proj"}),
            str(uuid4()),
            updated_ago,
        ),
    )
    row = await cursor.fetchone()
    assert row is not None
    return row[0]


async def _backdate_job(conn: psycopg.AsyncConnection, job_id: UUID, ago: timedelta) -> None:
    """Push a job's database-maintained ``updated_at`` back, past its BEFORE UPDATE trigger."""
    await conn.execute("ALTER TABLE jobs DISABLE TRIGGER jobs_set_updated_at")
    try:
        await conn.execute("UPDATE jobs SET updated_at = now() - %s WHERE id = %s", (ago, job_id))
    finally:
        await conn.execute("ALTER TABLE jobs ENABLE TRIGGER jobs_set_updated_at")


async def _reap(
    url: str,
    reapers: dict[str, CaptureReaper],
    *,
    settle: timedelta = _SETTLE,
    batch: int = DEFAULT_CAPTURE_REAP_BATCH,
    retry_base: timedelta = DEFAULT_CAPTURE_RETRY_BASE,
    retry_cap: timedelta = DEFAULT_CAPTURE_RETRY_CAP,
) -> int:
    """Run one pass on a non-autocommit connection, as the reconciler pool hands one over."""
    async with await psycopg.AsyncConnection.connect(url) as conn:
        return await reap_orphaned_captures(
            conn, reapers, settle=settle, batch=batch, retry_base=retry_base, retry_cap=retry_cap
        )


async def _reap_state(conn: psycopg.AsyncConnection, job_id: UUID) -> tuple | None:
    cursor = await conn.execute(
        "SELECT attempts, retry_after IS NOT NULL, reclaimed_at IS NOT NULL "
        "FROM capture_reap_state WHERE job_id = %s",
        (job_id,),
    )
    return await cursor.fetchone()


def test_reclaims_a_terminal_capture_past_the_settle_window(migrated_url: str) -> None:
    reaper = _Reaper()

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            system_id, run_id = await _seed_chain(conn, domain_name="kdive-stored")
            job_id = await _seed_capture_job(conn, run_id)

            assert await _reap(migrated_url, {_REMOTE: reaper}) == 1

            assert [capture.job_id for capture in reaper.seen] == [job_id]
            assert reaper.seen[0].provider_kind == _REMOTE
            assert reaper.seen[0].system_id == system_id
            assert reaper.seen[0].domain_name == "kdive-stored"
            seeded_name = await conn.execute(
                "SELECT name FROM resources WHERE id = %s", (reaper.seen[0].resource_id,)
            )
            assert await seeded_name.fetchone() == (reaper.seen[0].resource_name,)
            assert reaper.seen[0].resource_name.startswith("host-")
            assert await _reap_state(conn, job_id) == (1, False, True)

    asyncio.run(_run())


def test_derives_the_domain_name_only_when_the_system_stored_none(migrated_url: str) -> None:
    """The stored column wins; re-deriving would name the wrong domain for a named System."""
    reaper = _Reaper()

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            system_id, run_id = await _seed_chain(conn, domain_name=None)
            await _seed_capture_job(conn, run_id)

            assert await _reap(migrated_url, {_REMOTE: reaper}) == 1
            assert reaper.seen[0].domain_name == f"kdive-{system_id}"

    asyncio.run(_run())


@pytest.mark.parametrize("state", [JobState.FAILED, JobState.CANCELED, JobState.SUCCEEDED])
def test_selects_every_terminal_state_including_a_successful_capture(
    migrated_url: str, state: JobState
) -> None:
    """Reclaim is best-effort on both providers, so success is not evidence of removal."""
    reaper = _Reaper()

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            _, run_id = await _seed_chain(conn)
            job_id = await _seed_capture_job(conn, run_id, state=state)

            assert await _reap(migrated_url, {_REMOTE: reaper}) == 1
            assert [capture.job_id for capture in reaper.seen] == [job_id]

    asyncio.run(_run())


def test_leaves_a_running_capture_alone(migrated_url: str) -> None:
    reaper = _Reaper()

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            _, run_id = await _seed_chain(conn)
            running = await _seed_capture_job(conn, run_id, state=JobState.RUNNING)
            _, other_run = await _seed_chain(conn)
            terminal = await _seed_capture_job(conn, other_run)

            assert await _reap(migrated_url, {_REMOTE: reaper}) == 1

            assert [capture.job_id for capture in reaper.seen] == [terminal]
            assert await _reap_state(conn, running) is None

    asyncio.run(_run())


def test_leaves_a_row_inside_the_settle_window_alone(migrated_url: str) -> None:
    reaper = _Reaper()

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            _, fresh_run = await _seed_chain(conn)
            fresh = await _seed_capture_job(conn, fresh_run, updated_ago=timedelta(minutes=1))
            _, settled_run = await _seed_chain(conn)
            settled = await _seed_capture_job(conn, settled_run)

            assert await _reap(migrated_url, {_REMOTE: reaper}) == 1

            assert [capture.job_id for capture in reaper.seen] == [settled]
            assert await _reap_state(conn, fresh) is None

    asyncio.run(_run())


def test_an_idle_deployment_does_zero_work_on_a_second_pass(migrated_url: str) -> None:
    """A reclaimed row leaves the candidate set; convergence comes from the marker, not a window."""
    reaper = _Reaper()

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            _, run_id = await _seed_chain(conn)
            job_id = await _seed_capture_job(conn, run_id)

            assert await _reap(migrated_url, {_REMOTE: reaper}) == 1
            assert await _reap(migrated_url, {_REMOTE: reaper}) == 0

            assert [capture.job_id for capture in reaper.seen] == [job_id]
            assert await _reap_state(conn, job_id) == (1, False, True)

    asyncio.run(_run())


def test_a_row_of_a_kind_with_only_a_null_reaper_is_never_dispatched(migrated_url: str) -> None:
    """Disabled wiring is unregistered for eligibility: unmarked, unreclaimed, still observable."""
    remote = _Reaper()

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            _, local_run = await _seed_chain(conn, kind=_LOCAL)
            local_job = await _seed_capture_job(conn, local_run)
            _, remote_run = await _seed_chain(conn)
            remote_job = await _seed_capture_job(conn, remote_run)

            reaped = await _reap(migrated_url, {_LOCAL: NullCaptureReaper(), _REMOTE: remote})

            assert reaped == 1
            assert [capture.job_id for capture in remote.seen] == [remote_job]
            assert await _reap_state(conn, local_job) is None
            assert await _reap_state(conn, remote_job) == (1, False, True)

    asyncio.run(_run())


def test_no_registered_reaper_at_all_selects_nothing(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            _, run_id = await _seed_chain(conn)
            job_id = await _seed_capture_job(conn, run_id)

            assert await _reap(migrated_url, {}) == 0
            assert (
                await _reap(
                    migrated_url,
                    {_LOCAL: NullCaptureReaper(), _REMOTE: NullCaptureReaper()},
                )
                == 0
            )
            assert await _reap_state(conn, job_id) is None

    asyncio.run(_run())


def test_a_row_is_never_handed_to_another_providers_reaper(migrated_url: str) -> None:
    """Without the kind predicate a local row fails host binding on a remote reaper every pass."""
    local, remote = _Reaper(), _Reaper()

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            _, local_run = await _seed_chain(conn, kind=_LOCAL)
            local_job = await _seed_capture_job(conn, local_run)
            _, remote_run = await _seed_chain(conn)
            remote_job = await _seed_capture_job(conn, remote_run)

            assert await _reap(migrated_url, {_LOCAL: local, _REMOTE: remote}) == 2

            assert [capture.job_id for capture in local.seen] == [local_job]
            assert [capture.job_id for capture in remote.seen] == [remote_job]
            assert all(capture.provider_kind == _LOCAL for capture in local.seen)
            assert all(capture.provider_kind == _REMOTE for capture in remote.seen)

    asyncio.run(_run())


def test_a_backlog_larger_than_the_bound_drains_over_several_passes(migrated_url: str) -> None:
    reaper = _Reaper()

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            for _ in range(5):
                _, run_id = await _seed_chain(conn)
                await _seed_capture_job(conn, run_id)

            first = await _reap(migrated_url, {_REMOTE: reaper}, batch=2)
            second = await _reap(migrated_url, {_REMOTE: reaper}, batch=2)
            third = await _reap(migrated_url, {_REMOTE: reaper}, batch=2)
            fourth = await _reap(migrated_url, {_REMOTE: reaper}, batch=2)

            assert [first, second, third, fourth] == [2, 2, 1, 0]
            assert len({capture.job_id for capture in reaper.seen}) == 5
            marked = await conn.execute(
                "SELECT count(*) FROM capture_reap_state WHERE reclaimed_at IS NOT NULL"
            )
            assert await marked.fetchone() == (5,)

    asyncio.run(_run())


def test_a_resource_with_no_name_is_logged_and_deferred_not_raised(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    reaper = _Reaper()

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            system_id, run_id = await _seed_chain(conn, nameless=True)
            job_id = await _seed_capture_job(conn, run_id)

            with caplog.at_level(logging.WARNING):
                assert await _reap(migrated_url, {_REMOTE: reaper}) == 0

            assert reaper.seen == []
            assert await _reap_state(conn, job_id) == (1, True, False)
            assert any(
                str(job_id) in record.getMessage() and str(system_id) in record.getMessage()
                for record in caplog.records
            )

    asyncio.run(_run())


def test_one_rows_failure_does_not_prevent_the_rest_and_is_logged_with_its_identity(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            failing_system, failing_run = await _seed_chain(conn)
            failing_job = await _seed_capture_job(conn, failing_run)
            _, healthy_run = await _seed_chain(conn)
            healthy_job = await _seed_capture_job(conn, healthy_run)
            reaper = _Reaper(raises_for=frozenset({failing_job}))

            with caplog.at_level(logging.WARNING):
                assert await _reap(migrated_url, {_REMOTE: reaper}) == 1

            assert {capture.job_id for capture in reaper.seen} == {failing_job, healthy_job}
            assert await _reap_state(conn, healthy_job) == (1, False, True)
            assert await _reap_state(conn, failing_job) == (1, True, False)
            assert any(
                str(failing_job) in record.getMessage()
                and str(failing_system) in record.getMessage()
                for record in caplog.records
            )

    asyncio.run(_run())


def test_a_provider_that_declines_is_deferred_rather_than_marked(migrated_url: str) -> None:
    """A host no reachable reaper held reclaimed nothing, so counting it would be a false report."""
    reaper = _Reaper(reclaims=False)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            _, run_id = await _seed_chain(conn)
            job_id = await _seed_capture_job(conn, run_id)

            assert await _reap(migrated_url, {_REMOTE: reaper}) == 0

            assert [capture.job_id for capture in reaper.seen] == [job_id]
            assert await _reap_state(conn, job_id) == (1, True, False)

    asyncio.run(_run())


def test_a_deferred_row_waits_for_its_deadline_then_becomes_eligible(migrated_url: str) -> None:
    declining = _Reaper(reclaims=False)
    succeeding = _Reaper()

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            _, run_id = await _seed_chain(conn)
            job_id = await _seed_capture_job(conn, run_id)

            assert await _reap(migrated_url, {_REMOTE: declining}) == 0
            assert await _reap(migrated_url, {_REMOTE: succeeding}) == 0
            assert succeeding.seen == []

            await conn.execute(
                "UPDATE capture_reap_state SET retry_after = now() - interval '1 second' "
                "WHERE job_id = %s",
                (job_id,),
            )

            assert await _reap(migrated_url, {_REMOTE: succeeding}) == 1
            assert [capture.job_id for capture in succeeding.seen] == [job_id]
            assert await _reap_state(conn, job_id) == (2, False, True)

    asyncio.run(_run())


def test_each_failure_advances_the_deadline_past_its_prior_value_and_the_db_clock(
    migrated_url: str,
) -> None:
    reaper = _Reaper(reclaims=False)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            _, run_id = await _seed_chain(conn)
            job_id = await _seed_capture_job(conn, run_id)

            deadlines = []
            for _ in range(3):
                await _reap(migrated_url, {_REMOTE: reaper})
                cursor = await conn.execute(
                    "SELECT retry_after, retry_after > now(), attempts "
                    "FROM capture_reap_state WHERE job_id = %s",
                    (job_id,),
                )
                row = await cursor.fetchone()
                assert row is not None
                deadlines.append(row)
                # Re-arm eligibility without moving the recorded deadline backwards.
                await _backdate_job(conn, job_id, _PAST_SETTLE)
                await conn.execute(
                    "UPDATE capture_reap_state SET retry_after = now() - interval '1 second' "
                    "WHERE job_id = %s",
                    (job_id,),
                )

            assert [row[2] for row in deadlines] == [1, 2, 3]
            assert all(row[1] for row in deadlines), "each deadline must sit in the future"
            assert deadlines[0][0] < deadlines[1][0] < deadlines[2][0]

    asyncio.run(_run())


def test_the_backoff_is_bounded_by_the_configured_cap(migrated_url: str) -> None:
    reaper = _Reaper(reclaims=False)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            _, run_id = await _seed_chain(conn)
            job_id = await _seed_capture_job(conn, run_id)
            await conn.execute(
                "INSERT INTO capture_reap_state (job_id, attempts, retry_after) "
                "VALUES (%s, 12, now() - interval '1 second')",
                (job_id,),
            )

            assert (
                await _reap(
                    migrated_url,
                    {_REMOTE: reaper},
                    retry_base=timedelta(minutes=5),
                    retry_cap=timedelta(hours=1),
                )
                == 0
            )

            cursor = await conn.execute(
                "SELECT retry_after <= now() + interval '1 hour' + interval '5 seconds', "
                "       retry_after > now() + interval '55 minutes' "
                "FROM capture_reap_state WHERE job_id = %s",
                (job_id,),
            )
            assert await cursor.fetchone() == (True, True)

    asyncio.run(_run())


def test_an_untried_row_sorts_ahead_of_a_just_failed_one(migrated_url: str) -> None:
    """Otherwise one permanently failing old row comes back every pass ahead of fresh work."""
    reaper = _Reaper()

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            _, failed_run = await _seed_chain(conn)
            failed_job = await _seed_capture_job(conn, failed_run, updated_ago=timedelta(days=30))
            await conn.execute(
                "INSERT INTO capture_reap_state (job_id, attempts, retry_after) "
                "VALUES (%s, 9, now() - interval '1 hour')",
                (failed_job,),
            )
            _, untried_run = await _seed_chain(conn)
            untried_job = await _seed_capture_job(conn, untried_run)

            assert await _reap(migrated_url, {_REMOTE: reaper}, batch=1) == 1
            assert [capture.job_id for capture in reaper.seen] == [untried_job]

    asyncio.run(_run())


def test_a_live_owner_fence_defers_the_row_without_a_provider_call(migrated_url: str) -> None:
    """The positive ownership boundary, not the settle duration, is what holds the reaper off."""
    reaper = _Reaper()

    async def _run() -> None:
        async with (
            await _connect(migrated_url) as conn,
            await _connect(migrated_url) as owner,
        ):
            _, run_id = await _seed_chain(conn)
            job_id = await _seed_capture_job(conn, run_id)
            await owner.execute(
                "SELECT pg_advisory_lock(hashtextextended('kdive:job:' || %s::text, 1951))",
                (job_id,),
            )

            assert await _reap(migrated_url, {_REMOTE: reaper}) == 0
            assert reaper.seen == []
            # Deferred, not skipped: an unmarked row would sort ahead of every real candidate on
            # every later pass and hold its batch slot for as long as the owner stays wedged.
            assert await _reap_state(conn, job_id) == (1, True, False)

            await owner.execute(
                "SELECT pg_advisory_unlock(hashtextextended('kdive:job:' || %s::text, 1951))",
                (job_id,),
            )
            await conn.execute(
                "UPDATE capture_reap_state SET retry_after = now() - interval '1 second' "
                "WHERE job_id = %s",
                (job_id,),
            )

            assert await _reap(migrated_url, {_REMOTE: reaper}) == 1
            assert [capture.job_id for capture in reaper.seen] == [job_id]

    asyncio.run(_run())


def test_a_job_created_after_the_cutoff_with_no_attempt_link_stays_deferred(
    migrated_url: str,
) -> None:
    """A missing attempt link is fail-closed after the cutoff, not accepted as coverage."""
    reaper = _Reaper()

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            _, covered_run = await _seed_chain(conn)
            covered_job = await _seed_capture_job(conn, covered_run, before_cutoff=True)
            _, later_run = await _seed_chain(conn)
            later_job = await _seed_capture_job(conn, later_run, before_cutoff=False)

            assert await _reap(migrated_url, {_REMOTE: reaper}) == 1

            assert [capture.job_id for capture in reaper.seen] == [covered_job]
            assert await _reap_state(conn, later_job) is None
            assert await _reap_state(conn, covered_job) == (1, False, True)

    asyncio.run(_run())


def test_an_incomplete_cutover_generation_covers_no_pre_cutover_row(migrated_url: str) -> None:
    reaper = _Reaper()

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            _, run_id = await _seed_chain(conn)
            job_id = await _seed_capture_job(conn, run_id, before_cutoff=True)
            await conn.execute(
                "ALTER TABLE capture_operation_cutoff "
                "DROP CONSTRAINT capture_operation_cutoff_complete_check"
            )
            await conn.execute("UPDATE capture_operation_cutoff SET complete = false")

            assert await _reap(migrated_url, {_REMOTE: reaper}) == 0
            assert reaper.seen == []
            assert await _reap_state(conn, job_id) is None

            await conn.execute("UPDATE capture_operation_cutoff SET complete = true")
            assert await _reap(migrated_url, {_REMOTE: reaper}) == 1

    asyncio.run(_run())


def test_a_kind_with_no_registered_reaper_is_excluded_from_selection(migrated_url: str) -> None:
    """The kind predicate, not the dispatch table, is what keeps a foreign row out of the batch.

    Without it the sweep would select a fault-inject row and then look its kind up in a registry
    that has no entry for it, so the whole pass would die on the first such row instead of
    reclaiming the captures it can.
    """
    reaper = _Reaper()

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            _, foreign_run = await _seed_chain(conn, kind="fault-inject")
            foreign_job = await _seed_capture_job(conn, foreign_run)
            _, remote_run = await _seed_chain(conn)
            remote_job = await _seed_capture_job(conn, remote_run)

            assert await _reap(migrated_url, {_REMOTE: reaper}) == 1

            assert [capture.job_id for capture in reaper.seen] == [remote_job]
            assert await _reap_state(conn, foreign_job) is None

    asyncio.run(_run())


def test_a_deferral_advances_past_a_deadline_another_pass_wrote_first(migrated_url: str) -> None:
    """Two passes can both see one row eligible; the loser must not shorten the winner's deadline.

    Reproduces that race from inside the provider call, which is where the sweep is at its widest:
    the fenced transaction holds an advisory lock, not the (not yet existing) reap-state row, so a
    concurrent pass can commit a deadline between this pass's selection and its own write.
    """
    horizon = timedelta(hours=1)

    class _RacingReaper:
        def __init__(self) -> None:
            self.seen: list[OrphanedCapture] = []

        async def reclaim_capture(self, capture: OrphanedCapture) -> bool:
            self.seen.append(capture)
            async with await _connect(migrated_url) as other:
                await other.execute(
                    "INSERT INTO capture_reap_state (job_id, attempts, retry_after) "
                    "VALUES (%s, 1, now() + %s)",
                    (capture.job_id, horizon),
                )
            return False

    reaper = _RacingReaper()

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            _, run_id = await _seed_chain(conn)
            job_id = await _seed_capture_job(conn, run_id)

            assert await _reap(migrated_url, {_REMOTE: reaper}) == 0

            assert [capture.job_id for capture in reaper.seen] == [job_id]
            cursor = await conn.execute(
                "SELECT attempts, retry_after > now() + %s, retry_after > now() "
                "FROM capture_reap_state WHERE job_id = %s",
                (horizon, job_id),
            )
            assert await cursor.fetchone() == (2, True, True)

    asyncio.run(_run())


async def _seed_supervised_attempt(
    conn: psycopg.AsyncConnection,
    job_id: UUID,
    system_id: UUID,
    *,
    publication_state: str = "discarded",
    spool_disposed: bool = True,
    job_attempt: int = 1,
) -> None:
    """Link one exited supervised attempt to ``job_id`` with the publication state given."""
    incarnation = f"worker-{uuid4().hex[:12]}"
    await conn.execute(
        "INSERT INTO worker_incarnations (incarnation, authority_kind, authority_binding, "
        "    fence_protocol) VALUES (%s, 'local', %s, 4)",
        (incarnation, Jsonb({"host": "h"})),
    )
    resource = await conn.execute(
        "SELECT a.resource_id, res.kind FROM systems AS s "
        "JOIN allocations AS a ON a.id = s.allocation_id "
        "JOIN resources AS res ON res.id = a.resource_id WHERE s.id = %s",
        (system_id,),
    )
    row = await resource.fetchone()
    assert row is not None
    closed = publication_state in {"published", "discarded"}
    await conn.execute(
        "INSERT INTO capture_operations (job_id, job_attempt, worker_incarnation, provider_kind, "
        "    resource_id, system_id, domain_name, request_digest, launch_token, host_instance, "
        "    state, exit_outcome, exited_at, process_absent, provider_quiescence, "
        "    publication_state, publication_object_key, publication_tombstone_version, "
        "    publication_started_at, publication_closed_at, spool_disposed_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, 'kdive-guest', repeat('a', 64), %s, 'h', "
        "    'exited', 'exited', now(), true, %s, %s, %s, %s, %s, %s, %s)",
        (
            job_id,
            job_attempt,
            incarnation,
            row[1],
            row[0],
            system_id,
            uuid4().hex + uuid4().hex,
            Jsonb({"result": "absent", "ordering": "fresh-qmp-connection"}),
            publication_state,
            "captures/key" if publication_state != "pending" else None,
            "tombstone-1" if publication_state == "discarded" else None,
            "now()" if publication_state != "pending" else None,
            "now()" if closed else None,
            "now()" if spool_disposed and closed else None,
        ),
    )


def test_a_post_cutoff_row_needs_its_attempts_quiescence_and_publication_closure(
    migrated_url: str,
) -> None:
    """Positive attempt-linked evidence, not the settle window, is what admits a supervised row."""
    reaper = _Reaper()

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            system_id, run_id = await _seed_chain(conn)
            job_id = await _seed_capture_job(conn, run_id, before_cutoff=False)
            await _seed_supervised_attempt(conn, job_id, system_id)

            assert await _reap(migrated_url, {_REMOTE: reaper}) == 1
            assert [capture.job_id for capture in reaper.seen] == [job_id]

    asyncio.run(_run())


@pytest.mark.parametrize(
    "publication_state,spool_disposed",
    [("pending", False), ("discarded", False)],
)
def test_a_post_cutoff_row_with_an_open_attempt_product_stays_deferred(
    migrated_url: str, publication_state: str, spool_disposed: bool
) -> None:
    """An attempt that can still commit an artifact or hold a spool must not be reaped."""
    reaper = _Reaper()

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            system_id, run_id = await _seed_chain(conn)
            job_id = await _seed_capture_job(conn, run_id, before_cutoff=False)
            await _seed_supervised_attempt(
                conn,
                job_id,
                system_id,
                publication_state=publication_state,
                spool_disposed=spool_disposed,
            )

            assert await _reap(migrated_url, {_REMOTE: reaper}) == 0
            assert reaper.seen == []
            assert await _reap_state(conn, job_id) is None

    asyncio.run(_run())


def test_a_pre_cutoff_row_that_grew_an_attempt_link_uses_the_stronger_evidence(
    migrated_url: str,
) -> None:
    """Cutover coverage is for rows with no supervised attempt, not a bypass for open ones."""
    reaper = _Reaper()

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            system_id, run_id = await _seed_chain(conn)
            job_id = await _seed_capture_job(conn, run_id, before_cutoff=True)
            await _seed_supervised_attempt(conn, job_id, system_id, publication_state="pending")

            assert await _reap(migrated_url, {_REMOTE: reaper}) == 0
            assert reaper.seen == []

            await conn.execute(
                "UPDATE capture_operations SET publication_state = 'discarded', "
                "    publication_object_key = 'captures/key', "
                "    publication_tombstone_version = 'tombstone-1', "
                "    publication_started_at = now(), publication_closed_at = now(), "
                "    spool_disposed_at = now() WHERE job_id = %s",
                (job_id,),
            )

            assert await _reap(migrated_url, {_REMOTE: reaper}) == 1
            assert [capture.job_id for capture in reaper.seen] == [job_id]

    asyncio.run(_run())


def test_an_exited_attempt_without_quiescence_evidence_is_not_reaped(migrated_url: str) -> None:
    """The sweep demands quiescence itself rather than inheriting 0112's exit-shape constraint.

    That constraint makes an exited attempt with empty ``provider_quiescence`` unrepresentable, so
    it is dropped here to reach the sweep's own predicate. Without this the clause would be
    decorative: it could be deleted and every other test would still pass, and a later migration
    that loosened the exit shape would silently open the sweep to an attempt whose provider state
    was never proven quiet.
    """
    reaper = _Reaper()

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            system_id, run_id = await _seed_chain(conn)
            job_id = await _seed_capture_job(conn, run_id, before_cutoff=False)
            await _seed_supervised_attempt(conn, job_id, system_id)
            await conn.execute(
                "ALTER TABLE capture_operations DROP CONSTRAINT capture_operations_exit_shape"
            )
            await conn.execute(
                "UPDATE capture_operations SET provider_quiescence = '{}'::jsonb, "
                "    process_absent = false WHERE job_id = %s",
                (job_id,),
            )

            assert await _reap(migrated_url, {_REMOTE: reaper}) == 0
            assert reaper.seen == []
            assert await _reap_state(conn, job_id) is None

            await conn.execute(
                "UPDATE capture_operations SET provider_quiescence = %s, process_absent = true "
                "WHERE job_id = %s",
                (Jsonb({"result": "absent"}), job_id),
            )
            assert await _reap(migrated_url, {_REMOTE: reaper}) == 1

    asyncio.run(_run())


def test_the_sweep_runs_under_the_real_reconciler_role(migrated_url: str) -> None:
    """Every grant the sweep needs, exercised as the process principal rather than a superuser.

    The rest of this module connects as the test superuser, so a missing grant would only ever
    surface in a deployed reconciler. This drives the whole path — the five-table ownership join,
    the guarded capture_operations and capture_operation_cutoff column reads, and the reap-state
    write — through a login that holds only kdive_reconciler.
    """
    reaper = _Reaper()
    login = f"kdive_reconciler_capture_{uuid4().hex[:10]}"

    async def _run() -> None:
        async with await _connect(migrated_url) as admin:
            _, run_id = await _seed_chain(admin)
            job_id = await _seed_capture_job(admin, run_id)
            await admin.execute(
                SQL("CREATE ROLE {} LOGIN PASSWORD {} IN ROLE kdive_reconciler").format(
                    Identifier(login), Literal(_ROLE_AUTHENTICATION)
                )
            )
            try:
                role_url = make_conninfo(
                    **{
                        **admin.info.get_parameters(),
                        "user": login,
                        "password": _ROLE_AUTHENTICATION,
                    }
                )
                async with await psycopg.AsyncConnection.connect(role_url) as conn:
                    reaped = await reap_orphaned_captures(
                        conn,
                        {_REMOTE: reaper},
                        settle=_SETTLE,
                        batch=DEFAULT_CAPTURE_REAP_BATCH,
                        retry_base=DEFAULT_CAPTURE_RETRY_BASE,
                        retry_cap=DEFAULT_CAPTURE_RETRY_CAP,
                    )
                assert reaped == 1
                assert [capture.job_id for capture in reaper.seen] == [job_id]
                assert await _reap_state(admin, job_id) == (1, False, True)
            finally:
                await admin.execute(SQL("DROP ROLE IF EXISTS {}").format(Identifier(login)))

    asyncio.run(_run())


def test_a_retried_job_is_still_governed_by_an_earlier_attempts_open_product(
    migrated_url: str,
) -> None:
    """Cutover coverage must mean "no supervised attempt at all", not "none for this attempt".

    A capture queued across the 0113 upgrade survives its cutover (0112 only cancels running
    rows), so it sits behind ``cutoff_at``. Attempt 1 opens a supervised operation and its worker
    dies with publication still ``pending`` and the spool undisposed; the job retries to attempt 2,
    dies before creating its own operation, and exhausts its retries into ``failed``. The
    attempt-linked branch cannot admit it — there is no row for attempt 2 — so if the cutover
    branch only asks about the *current* attempt, the row is dispatched while attempt 1's
    publication can still commit an artifact and still needs its object.
    """
    reaper = _Reaper()

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            system_id, run_id = await _seed_chain(conn)
            job_id = await _seed_capture_job(conn, run_id, before_cutoff=True, attempt=2)
            await _seed_supervised_attempt(
                conn,
                job_id,
                system_id,
                publication_state="pending",
                spool_disposed=False,
                job_attempt=1,
            )

            assert await _reap(migrated_url, {_REMOTE: reaper}) == 0
            assert reaper.seen == []
            assert await _reap_state(conn, job_id) is None

    asyncio.run(_run())


def test_a_retried_job_whose_earlier_attempt_closed_is_still_not_cutover_covered(
    migrated_url: str,
) -> None:
    """A job that ever had a supervised attempt has left the pre-cutover population for good.

    Attempt 1 closed cleanly, so nothing is at risk — but the job is no longer one of the rows
    that "predate supervision and can never grow an attempt link", and admitting it through the
    cutover branch would be admitting it on absence of evidence for attempt 2 rather than on
    evidence. It becomes eligible when its own attempt closes, not before.
    """
    reaper = _Reaper()

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            system_id, run_id = await _seed_chain(conn)
            job_id = await _seed_capture_job(conn, run_id, before_cutoff=True, attempt=2)
            await _seed_supervised_attempt(conn, job_id, system_id, job_attempt=1)

            assert await _reap(migrated_url, {_REMOTE: reaper}) == 0
            assert reaper.seen == []

            await _seed_supervised_attempt(conn, job_id, system_id, job_attempt=2)

            assert await _reap(migrated_url, {_REMOTE: reaper}) == 1
            assert [capture.job_id for capture in reaper.seen] == [job_id]

    asyncio.run(_run())


def test_the_attempt_linked_branch_reads_only_the_jobs_current_attempt(migrated_url: str) -> None:
    """A closed attempt 1 must not vouch for an open attempt 2 on the same job."""
    reaper = _Reaper()

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            system_id, run_id = await _seed_chain(conn)
            job_id = await _seed_capture_job(conn, run_id, before_cutoff=False, attempt=2)
            await _seed_supervised_attempt(conn, job_id, system_id, job_attempt=1)
            await _seed_supervised_attempt(
                conn,
                job_id,
                system_id,
                publication_state="pending",
                spool_disposed=False,
                job_attempt=2,
            )

            assert await _reap(migrated_url, {_REMOTE: reaper}) == 0
            assert reaper.seen == []

    asyncio.run(_run())


def test_wedged_fenced_rows_do_not_hold_the_batch_against_eligible_work(migrated_url: str) -> None:
    """A batch full of fenced rows must not starve a reclaimable one on every later pass."""
    reaper = _Reaper()

    async def _run() -> None:
        async with (
            await _connect(migrated_url) as conn,
            await _connect(migrated_url) as owner,
        ):
            for _ in range(2):
                _, wedged_run = await _seed_chain(conn)
                wedged_job = await _seed_capture_job(conn, wedged_run)
                await owner.execute(
                    "SELECT pg_advisory_lock(hashtextextended('kdive:job:' || %s::text, 1951))",
                    (wedged_job,),
                )
            _, healthy_run = await _seed_chain(conn)
            healthy_job = await _seed_capture_job(conn, healthy_run)

            # The batch is the size of the wedged set, so on the first pass the fenced rows can
            # fill it entirely and the healthy row may not be reached at all.
            assert await _reap(migrated_url, {_REMOTE: reaper}, batch=2) == 0

            # Having taken deadlines, they drop behind the untouched row and it is reached next.
            assert await _reap(migrated_url, {_REMOTE: reaper}, batch=2) == 1
            assert [capture.job_id for capture in reaper.seen] == [healthy_job]

    asyncio.run(_run())
