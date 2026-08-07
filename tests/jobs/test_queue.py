"""Tests for the connection-scoped queue operations (ADR-0018)."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from datetime import timedelta
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from kdive.domain.capacity.state import JobState
from kdive.domain.errors import ErrorCategory
from kdive.domain.operations.jobs import (
    DEFAULT_JOB_DISPATCH_LANE,
    RETIRED_JOB_KINDS,
    STATE_FENCED_JOB_DISPATCH_LANE,
    STATE_FENCED_JOB_KINDS,
    Job,
    JobKind,
)
from kdive.jobs import queue
from kdive.jobs.payloads import (
    AuthorizeSshKeyPayload,
    Authorizing,
    InstallPayload,
    ReprovisionPayload,
    RestorePayload,
    SnapshotPayload,
    SystemPayload,
)
from kdive.services.runs.worker_incarnations import CURRENT_WORKER_FENCE_PROTOCOL

_AUTHORIZING = Authorizing(principal="p", agent_session=None, project="a")


def _build_payload() -> InstallPayload:
    return InstallPayload(run_id=str(uuid4()))


def _install_payload(run_id: str, cmdline: str | None = None) -> InstallPayload:
    return InstallPayload(run_id=run_id, cmdline=cmdline)


def _system_payload() -> SystemPayload:
    return SystemPayload(system_id=str(uuid4()))


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


async def _count_jobs(conn: psycopg.AsyncConnection) -> int:
    cur = await conn.execute("SELECT count(*) FROM jobs")
    row = await cur.fetchone()
    assert row is not None  # COUNT(*) always returns one row
    return row[0]


def _credential(value: str) -> SecretStr:
    return SecretStr(hashlib.sha256(value.encode()).hexdigest())


async def _register_worker(
    conn: psycopg.AsyncConnection,
    worker_id: str,
    *,
    credential: SecretStr | None = None,
    protocol: int = CURRENT_WORKER_FENCE_PROTOCOL,
    terminated: bool = False,
) -> SecretStr:
    credential = credential or _credential(worker_id)
    await conn.execute(
        "INSERT INTO worker_incarnations (incarnation, authority_kind, authority_binding, "
        "fence_protocol, credential_hash, state, terminated_at, outcome) VALUES "
        "(%s, 'local', '{}'::jsonb, %s, sha256(convert_to(%s, 'UTF8')), %s, "
        "CASE WHEN %s THEN clock_timestamp() END, CASE WHEN %s THEN 'killed' END) "
        "ON CONFLICT (incarnation) DO NOTHING",
        (
            worker_id,
            protocol,
            credential.get_secret_value(),
            "terminated" if terminated else "active",
            terminated,
            terminated,
        ),
    )
    return credential


async def _dequeue(
    conn: psycopg.AsyncConnection,
    worker_id: str,
    *,
    incarnation_credential: SecretStr | None = None,
    lease: timedelta = queue.DEFAULT_LEASE,
    accepted_lanes: Sequence[str] = queue.DEFAULT_DISPATCH_LANES,
) -> Job | None:
    credential = (
        incarnation_credential
        if incarnation_credential is not None
        else await _register_worker(conn, worker_id)
    )
    return await queue.dequeue(
        conn,
        worker_id,
        incarnation_credential=credential,
        lease=lease,
        accepted_lanes=accepted_lanes,
    )


def test_enqueue_inserts_queued_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            payload = _build_payload()
            authorizing = Authorizing(principal="alice", agent_session=None, project="kernel-team")
            job = await queue.enqueue(conn, JobKind.INSTALL, payload, authorizing, "dk-1")
            assert isinstance(job, Job)
            assert job.state is JobState.QUEUED
            assert job.attempt == 0
            assert job.payload == payload.model_dump(mode="json", exclude_none=True)
            assert job.authorizing == authorizing.model_dump(mode="json")
            assert job.dedup_key == "dk-1"
            assert await _count_jobs(conn) == 1

    asyncio.run(_run())


def test_enqueue_same_dedup_key_returns_same_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            first = await queue.enqueue(
                conn, JobKind.INSTALL, _build_payload(), _AUTHORIZING, "dk-dup"
            )
            second = await queue.enqueue(
                conn,
                JobKind.PROVISION,
                _system_payload(),
                Authorizing(principal="p", project="b"),
                "dk-dup",
            )
            assert second.id == first.id
            assert second.kind is JobKind.INSTALL  # the existing row, unchanged
            assert await _count_jobs(conn) == 1

    asyncio.run(_run())


def test_enqueue_distinct_dedup_keys_make_distinct_jobs(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            a = await queue.enqueue(conn, JobKind.INSTALL, _build_payload(), _AUTHORIZING, "dk-a")
            b = await queue.enqueue(conn, JobKind.INSTALL, _build_payload(), _AUTHORIZING, "dk-b")
            assert a.id != b.id
            assert await _count_jobs(conn) == 2

    asyncio.run(_run())


def test_get_by_dedup_key_returns_the_enqueued_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            enqueued = await queue.enqueue(
                conn, JobKind.PROVISION, _system_payload(), _AUTHORIZING, "alloc:provision"
            )
            found = await queue.get_by_dedup_key(conn, "alloc:provision")
            assert found is not None
            assert found.id == enqueued.id
            assert found.dedup_key == "alloc:provision"

    asyncio.run(_run())


def test_get_by_dedup_key_returns_none_for_unknown_key(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            assert await queue.get_by_dedup_key(conn, "nope:provision") is None

    asyncio.run(_run())


def test_enqueue_rejects_max_attempts_below_one(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            with pytest.raises(ValueError, match="max_attempts"):
                await queue.enqueue(
                    conn, JobKind.INSTALL, _build_payload(), _AUTHORIZING, "dk-0", max_attempts=0
                )

    asyncio.run(_run())


def test_enqueue_rejects_canceled_recycling_without_terminal_recycling(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            with pytest.raises(ValueError, match="recycle_canceled requires recycle_terminal"):
                await queue.enqueue(
                    conn,
                    JobKind.INSTALL,
                    _build_payload(),
                    _AUTHORIZING,
                    "dk-invalid-recycle",
                    recycle_canceled=True,
                )

    asyncio.run(_run())


def _fenced_payload(kind: JobKind) -> Any:
    """A minimal valid payload for each state-fenced kind (ADR-0550)."""
    system_id = str(uuid4())
    if kind is JobKind.SNAPSHOT:
        return SnapshotPayload(
            system_id=system_id, snapshot_id=str(uuid4()), name="snap", include_memory=False
        )
    if kind is JobKind.RESTORE:
        return RestorePayload(system_id=system_id, name="snap", start_paused=False)
    return ReprovisionPayload(system_id=system_id, profile_digest="sha256:abc")


@pytest.mark.parametrize(
    "kind", sorted(STATE_FENCED_JOB_KINDS, key=lambda kind: cast(JobKind, kind).value)
)
def test_enqueue_routes_state_fenced_kinds_to_the_fenced_lane(
    migrated_url: str, kind: JobKind
) -> None:
    """S1: the three fencing kinds land on the fenced lane, derived from the kind alone."""

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            job = await queue.enqueue(
                conn, kind, _fenced_payload(kind), _AUTHORIZING, f"dk-fenced-{kind.value}"
            )
            assert job.dispatch_lane == STATE_FENCED_JOB_DISPATCH_LANE

    asyncio.run(_run())


def test_enqueue_routes_a_non_fenced_kind_to_the_default_lane(migrated_url: str) -> None:
    """S2, through-enqueue half. The derivation over every kind is covered in tests/domain."""

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            job = await queue.enqueue(
                conn, JobKind.INSTALL, _build_payload(), _AUTHORIZING, "dk-default-lane"
            )
            assert job.dispatch_lane == DEFAULT_JOB_DISPATCH_LANE

    asyncio.run(_run())


def test_recycle_reroutes_a_row_first_inserted_on_another_lane(migrated_url: str) -> None:
    """S9: a restore row created before ADR-0550 must not keep recycling onto `default`.

    ``systems.restore`` recycles under a durable ``dedup_key``, so a row left on its original
    lane would stay there for every future attempt — the fix would silently never reach a System
    that had already used the feature.
    """

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            payload = _fenced_payload(JobKind.RESTORE)
            job = await queue.enqueue(
                conn, JobKind.RESTORE, payload, _AUTHORIZING, "dk-recycle-lane"
            )
            # Simulate the pre-upgrade row: on `default`, and terminal so the recycle fires.
            await conn.execute(
                "UPDATE jobs SET dispatch_lane = %s, state = %s WHERE id = %s",
                (DEFAULT_JOB_DISPATCH_LANE, JobState.FAILED.value, job.id),
            )
            before = await _row(conn, job.id)

            recycled = await queue.enqueue(
                conn,
                JobKind.RESTORE,
                payload,
                _AUTHORIZING,
                "dk-recycle-lane",
                recycle_terminal=True,
            )

            assert recycled.id == job.id
            assert recycled.state is JobState.QUEUED
            assert recycled.dispatch_lane == STATE_FENCED_JOB_DISPATCH_LANE
            # ADR-0447's re-dating still holds; this test must not silently drop that guard.
            after = await _row(conn, job.id)
            assert after["created_at"] > before["created_at"]

    asyncio.run(_run())


async def _set_lane(conn: psycopg.AsyncConnection, job_id: Any, lane: str) -> None:
    """Put a job on an arbitrary lane. ``enqueue`` derives the lane, so tests of the *dequeue*
    boundary write the column directly rather than asking ``enqueue`` for a lane it no longer
    accepts (ADR-0550)."""
    await conn.execute("UPDATE jobs SET dispatch_lane = %s WHERE id = %s", (lane, job_id))


async def _row(conn: psycopg.AsyncConnection, job_id: Any) -> dict[str, Any]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
        row = await cur.fetchone()
    assert row is not None
    return row


# Named rather than sorted inline: inside the decorator the element type comes from
# parametrize's argvalues, which widens to `object` and hides `JobKind.value`.
_RETIRED_KINDS: list[JobKind] = sorted(RETIRED_JOB_KINDS, key=lambda kind: kind.value)


@pytest.mark.parametrize("kind", _RETIRED_KINDS)
def test_enqueue_rejects_retired_job_kinds(migrated_url: str, kind: JobKind) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            with pytest.raises(ValueError, match="retired"):
                await queue.enqueue(conn, kind, _build_payload(), _AUTHORIZING, f"dk-{kind.value}")

    asyncio.run(_run())


def test_dequeue_claims_only_accepted_dispatch_lanes(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            # `enqueue` derives the lane from the kind (ADR-0550), so an arbitrary lane is set
            # on the column directly. The boundary under test is `dequeue`'s, not `enqueue`'s.
            first = await queue.enqueue(
                conn, JobKind.INSTALL, _build_payload(), _AUTHORIZING, "dk-provider-a"
            )
            await _set_lane(conn, first.id, "provider-a")
            second = await queue.enqueue(
                conn, JobKind.INSTALL, _build_payload(), _AUTHORIZING, "dk-provider-b"
            )
            await _set_lane(conn, second.id, "provider-b")
            skipped = await _dequeue(conn, "w-a", accepted_lanes=("provider-c",))
            assert skipped is None
            assert await queue.count_claimable(conn, accepted_lanes=("provider-c",)) == 0
            assert await queue.count_claimable(conn, accepted_lanes=("provider-b",)) == 1

            claimed = await _dequeue(conn, "w-b", accepted_lanes=("provider-b",))
            assert claimed is not None
            assert claimed.id == second.id
            assert claimed.dispatch_lane == "provider-b"

    asyncio.run(_run())


def test_dequeue_current_active_worker_claims_queued_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            queued = await queue.enqueue(
                conn, JobKind.INSTALL, _build_payload(), _AUTHORIZING, "dk-current-worker"
            )
            credential = await _register_worker(conn, "current-worker")

            claimed = await _dequeue(
                conn,
                "current-worker",
                incarnation_credential=credential,
            )

            assert claimed is not None
            assert claimed.id == queued.id
            assert claimed.state is JobState.RUNNING
            assert claimed.worker_id == "current-worker"
            assert claimed.attempt == 1

    asyncio.run(_run())


@pytest.mark.parametrize(
    "identity",
    ["missing", "malformed", "wrong", "terminated", "old-protocol"],
)
def test_dequeue_refuses_worker_without_current_active_credential(
    migrated_url: str, identity: str
) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            queued = await queue.enqueue(
                conn, JobKind.INSTALL, _build_payload(), _AUTHORIZING, f"dk-refused-{identity}"
            )
            worker_id = f"refused-{identity}"
            supplied = SecretStr("") if identity == "missing" else SecretStr("malformed")
            if identity == "wrong":
                await _register_worker(conn, worker_id)
                supplied = await _register_worker(conn, "another-worker")
            elif identity == "terminated":
                supplied = await _register_worker(conn, worker_id, terminated=True)
            elif identity == "old-protocol":
                supplied = await _register_worker(
                    conn,
                    worker_id,
                    protocol=CURRENT_WORKER_FENCE_PROTOCOL - 1,
                )

            assert (
                await _dequeue(
                    conn,
                    worker_id,
                    incarnation_credential=supplied,
                )
                is None
            )
            persisted = await (
                await conn.execute(
                    "SELECT state, worker_id, attempt FROM jobs WHERE id = %s", (queued.id,)
                )
            ).fetchone()
            assert persisted == (JobState.QUEUED.value, None, 0)

    asyncio.run(_run())


def test_worker_role_direct_old_style_claim_is_denied(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            queued = await queue.enqueue(
                conn, JobKind.INSTALL, _build_payload(), _AUTHORIZING, "dk-old-style-update"
            )
            with pytest.raises(
                psycopg.errors.InsufficientPrivilege,
                match="current active worker fence protocol",
            ):
                await conn.execute(
                    "UPDATE jobs SET state = 'running', worker_id = 'old-worker', "
                    "attempt = attempt + 1, lease_expires_at = now() + interval '5 minutes' "
                    "WHERE id = %s",
                    (queued.id,),
                )
            await conn.execute("SET SESSION AUTHORIZATION kdive_worker")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await conn.execute(
                    "UPDATE jobs SET state = 'running', worker_id = 'old-worker', "
                    "attempt = attempt + 1, lease_expires_at = now() + interval '5 minutes' "
                    "WHERE id = %s",
                    (queued.id,),
                )
            await conn.execute("RESET SESSION AUTHORIZATION")
            persisted = await (
                await conn.execute(
                    "SELECT state, worker_id, attempt FROM jobs WHERE id = %s", (queued.id,)
                )
            ).fetchone()
            assert persisted == (JobState.QUEUED.value, None, 0)

    asyncio.run(_run())


async def _terminal_failed_job(conn: psycopg.AsyncConnection, dedup_key: str) -> Job:
    """Create a job dead-lettered to ``failed`` at ``attempt == max_attempts`` (ADR-0185)."""
    credential = await _register_worker(conn, "w1")
    claimed = await _insert_running_job(
        conn, dedup_key, worker_id="w1", lease_seconds=300, attempt=3, max_attempts=3
    )
    failed = await queue.fail(
        conn,
        claimed,
        ErrorCategory.TRANSPORT_FAILURE,
        incarnation_credential=credential,
        failure_context={"failure_message": "blip"},
    )
    assert failed.state is JobState.FAILED
    return failed


def test_enqueue_recycle_terminal_resets_failed_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            failed = await _terminal_failed_job(conn, "dk-retry")

            recycled = await queue.enqueue(
                conn,
                JobKind.INSTALL,
                _build_payload(),
                _AUTHORIZING,
                "dk-retry",
                recycle_terminal=True,
            )

            assert recycled.id == failed.id  # reset in place, not replaced
            assert recycled.state is JobState.QUEUED
            assert recycled.attempt == 0
            assert recycled.worker_id is None
            assert recycled.lease_expires_at is None
            assert recycled.error_category is None
            assert recycled.failure_context == {}
            assert recycled.result_ref is None
            assert await _count_jobs(conn) == 1
            # No longer wedged: a worker can claim the recycled job.
            claimed = await _dequeue(conn, "w2")
            assert claimed is not None
            assert claimed.id == failed.id
            assert claimed.attempt == 1

    asyncio.run(_run())


def test_enqueue_recycle_terminal_does_not_preempt_newer_work(migrated_url: str) -> None:
    # ADR-0447 (#1528): `dequeue` orders by `created_at`, so a recycle that left `created_at` at
    # the original creation put the revived job ahead of everything enqueued since — and because
    # the recycle also resets `attempt`, a job that kept failing kept winning the claim, blocking
    # the lane head. A recycled job takes its place at the back of the lane instead.
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            stale = await _terminal_failed_job(conn, "dk-stale")
            await conn.execute(
                "UPDATE jobs SET created_at = now() - interval '1 hour' WHERE id = %s",
                (stale.id,),
            )

            # Enqueued after the recycled job's *original* creation, before the recycle.
            newer = await queue.enqueue(
                conn, JobKind.INSTALL, _build_payload(), _AUTHORIZING, "dk-newer"
            )

            recycled = await queue.enqueue(
                conn,
                JobKind.INSTALL,
                _build_payload(),
                _AUTHORIZING,
                "dk-stale",
                recycle_terminal=True,
            )
            assert recycled.id == stale.id  # still reset in place, not replaced
            assert recycled.created_at > newer.created_at  # re-dated to the recycle

            first = await _dequeue(conn, "w-order")
            assert first is not None
            assert first.id == newer.id  # the newer job is not starved

            second = await _dequeue(conn, "w-order")
            assert second is not None
            assert second.id == stale.id  # the recycled job still runs, just behind

    asyncio.run(_run())


def test_enqueue_stamps_a_first_insert_at_the_insert_not_the_transaction(migrated_url: str) -> None:
    # The INSERT stamps `clock_timestamp()` rather than taking the column's `DEFAULT now()`, for
    # the same reason the recycle does (ADR-0447): a caller opens a transaction and then blocks on
    # an advisory lock before enqueuing, so `transaction_timestamp()` would date a first enqueue to
    # before its own lock wait and let it preempt everything admitted during that wait.
    async def _run() -> None:
        inserter = await psycopg.AsyncConnection.connect(migrated_url)  # autocommit off
        try:
            async with inserter.transaction():
                # Fix this transaction's transaction_timestamp before the competing enqueue, the
                # way an advisory-lock wait does.
                await inserter.execute("SELECT 1")

                async with await _connect(migrated_url) as other:
                    competitor = await queue.enqueue(
                        other, JobKind.INSTALL, _build_payload(), _AUTHORIZING, "dk-first-other"
                    )

                inserted = await queue.enqueue(
                    inserter, JobKind.INSTALL, _build_payload(), _AUTHORIZING, "dk-first-late"
                )
                assert inserted.created_at > competitor.created_at
        finally:
            await inserter.close()

        async with await _connect(migrated_url) as reader:
            first = await _dequeue(reader, "w-first")
            assert first is not None
            assert first.id == competitor.id  # not preempted by the later insert

    asyncio.run(_run())


def test_enqueue_recycle_terminal_redates_past_a_concurrent_enqueue(migrated_url: str) -> None:
    # Every production caller reaches `enqueue` inside a transaction its own caller opened, after
    # blocking on an advisory lock — so `now()` (= transaction_timestamp) would stamp the revived
    # job at the transaction's *start*, leaving it ahead of everything enqueued during the wait.
    # `clock_timestamp()` re-dates at the recycle itself. This drives the recycle on a
    # non-autocommit connection in one explicit transaction, with the competing job admitted by a
    # separate connection after that transaction opened.
    async def _run() -> None:
        async with await _connect(migrated_url) as setup:
            stale = await _terminal_failed_job(setup, "dk-txn-stale")

        recycler = await psycopg.AsyncConnection.connect(migrated_url)  # autocommit off
        try:
            async with recycler.transaction():
                # Open the transaction — and so fix its transaction_timestamp — before the
                # competing enqueue, the way an advisory-lock wait does.
                await recycler.execute("SELECT 1")

                async with await _connect(migrated_url) as other:
                    newer = await queue.enqueue(
                        other, JobKind.INSTALL, _build_payload(), _AUTHORIZING, "dk-txn-newer"
                    )

                recycled = await queue.enqueue(
                    recycler,
                    JobKind.INSTALL,
                    _build_payload(),
                    _AUTHORIZING,
                    "dk-txn-stale",
                    recycle_terminal=True,
                )
                assert recycled.id == stale.id
                assert recycled.created_at > newer.created_at
        finally:
            await recycler.close()

        async with await _connect(migrated_url) as reader:
            first = await _dequeue(reader, "w-txn")
            assert first is not None
            assert first.id == newer.id  # not preempted by the job recycled after it

    asyncio.run(_run())


def test_enqueue_recycle_terminal_preserves_in_flight(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            # A queued in-flight job is deduped, not reset.
            queued = await queue.enqueue(
                conn, JobKind.INSTALL, _build_payload(), _AUTHORIZING, "dk-q"
            )
            again = await queue.enqueue(
                conn,
                JobKind.INSTALL,
                _build_payload(),
                _AUTHORIZING,
                "dk-q",
                recycle_terminal=True,
            )
            assert again.id == queued.id
            assert again.state is JobState.QUEUED
            assert again.attempt == 0

            # A running job keeps its worker and lease (the fence excludes 'running').
            running = await _insert_running_job(conn, "dk-run", worker_id="w1", lease_seconds=300)
            held = await queue.enqueue(
                conn,
                JobKind.INSTALL,
                _build_payload(),
                _AUTHORIZING,
                "dk-run",
                recycle_terminal=True,
            )
            assert held.id == running.id
            assert held.state is JobState.RUNNING
            assert held.worker_id == "w1"
            assert held.lease_expires_at is not None

    asyncio.run(_run())


def test_enqueue_recycle_terminal_resets_succeeded_job_with_new_payload(migrated_url: str) -> None:
    # ADR-0299: a re-stage recycles a *succeeded* install job in place, overwriting its payload
    # (the new cmdline) and clearing result_ref — otherwise the re-run boots the prior cmdline.
    async def _run() -> None:
        run_id = str(uuid4())
        async with await _connect(migrated_url) as conn:
            first = await queue.enqueue(
                conn, JobKind.INSTALL, _install_payload(run_id, "a=1"), _AUTHORIZING, "dk-restage"
            )
            claimed = await _dequeue(conn, "w1")
            assert claimed is not None
            done = await queue.complete(
                conn,
                claimed.id,
                "result-ref",
                attempt=claimed.attempt,
                incarnation_credential=_credential("w1"),
            )
            assert done is not None and done.state is JobState.SUCCEEDED

            recycled = await queue.enqueue(
                conn,
                JobKind.INSTALL,
                _install_payload(run_id, "a=2"),
                _AUTHORIZING,
                "dk-restage",
                recycle_terminal=True,
            )
            assert recycled.id == first.id  # reset in place
            assert recycled.state is JobState.QUEUED
            assert recycled.payload["cmdline"] == "a=2"  # payload overwritten
            assert recycled.result_ref is None  # success field cleared
            assert await _count_jobs(conn) == 1

    asyncio.run(_run())


def test_enqueue_recycle_canceled_reclaims_only_when_opted_in(migrated_url: str) -> None:
    # A stable-dedup-key caller re-issued after an explicit cancel (control.watch_for_crash,
    # ADR-0367) reclaims the wedged slot only with recycle_canceled; recycle_terminal alone keeps
    # the no-resurrection-of-canceled default.
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            job = await queue.enqueue(
                conn, JobKind.INSTALL, _build_payload(), _AUTHORIZING, "dk-cancel"
            )
            await conn.execute("UPDATE jobs SET state = 'canceled' WHERE id = %s", (job.id,))

            kept = await queue.enqueue(
                conn,
                JobKind.INSTALL,
                _build_payload(),
                _AUTHORIZING,
                "dk-cancel",
                recycle_terminal=True,
            )
            assert kept.id == job.id and kept.state is JobState.CANCELED  # invariant preserved

            reclaimed = await queue.enqueue(
                conn,
                JobKind.INSTALL,
                _build_payload(),
                _AUTHORIZING,
                "dk-cancel",
                recycle_terminal=True,
                recycle_canceled=True,
            )
            assert reclaimed.id == job.id  # reset in place, not a duplicate
            assert reclaimed.state is JobState.QUEUED
            assert reclaimed.attempt == 0
            assert await _count_jobs(conn) == 1

    asyncio.run(_run())


def test_enqueue_default_leaves_succeeded_job_untouched(migrated_url: str) -> None:
    # Without the flag, a succeeded job is never resurrected (no-resurrection default holds).
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await queue.enqueue(conn, JobKind.INSTALL, _build_payload(), _AUTHORIZING, "dk-ok")
            claimed = await _dequeue(conn, "w1")
            assert claimed is not None
            done = await queue.complete(
                conn,
                claimed.id,
                "result-ref",
                attempt=claimed.attempt,
                incarnation_credential=_credential("w1"),
            )
            assert done is not None and done.state is JobState.SUCCEEDED

            again = await queue.enqueue(
                conn, JobKind.INSTALL, _build_payload(), _AUTHORIZING, "dk-ok"
            )
            assert again.id == claimed.id
            assert again.state is JobState.SUCCEEDED  # not resurrected without the flag
            assert again.result_ref == "result-ref"

    asyncio.run(_run())


def test_enqueue_default_leaves_failed_job_untouched(migrated_url: str) -> None:
    # The default (no flag) preserves today's behavior for provision/install dedup keys (ADR-0149).
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            failed = await _terminal_failed_job(conn, "dk-prov")

            same = await queue.enqueue(
                conn, JobKind.PROVISION, _system_payload(), _AUTHORIZING, "dk-prov"
            )

            assert same.id == failed.id
            assert same.state is JobState.FAILED  # untouched: still dead-lettered
            assert same.error_category is ErrorCategory.TRANSPORT_FAILURE

    asyncio.run(_run())


async def _insert_running_job(
    conn: psycopg.AsyncConnection,
    dedup_key: str,
    *,
    worker_id: str = "dead",
    lease_seconds: int,
    attempt: int = 0,
    max_attempts: int = 3,
) -> Job:
    """Insert a job already in ``running`` with a lease ``lease_seconds`` from now.

    Negative ``lease_seconds`` makes the lease already lapsed. The timestamp is
    computed in SQL (``now() + make_interval(...)``) — a relative interval cannot be
    passed as a bound parameter to a ``timestamptz`` column.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "INSERT INTO jobs (kind, payload, state, attempt, max_attempts, worker_id, "
            "    lease_expires_at, authorizing, dedup_key) "
            "VALUES (%s, %s, 'running', %s, %s, %s, now() + make_interval(secs => %s), "
            "    %s, %s) RETURNING *",
            (
                JobKind.INSTALL.value,
                Jsonb(_build_payload().model_dump(mode="json", exclude_none=True)),
                attempt,
                max_attempts,
                worker_id,
                lease_seconds,
                Jsonb(_AUTHORIZING.model_dump(mode="json")),
                dedup_key,
            ),
        )
        row = await cur.fetchone()
    return Job.model_validate(row)


def test_dequeue_claims_oldest_and_charges_attempt(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            old = await queue.enqueue(
                conn, JobKind.INSTALL, _build_payload(), _AUTHORIZING, "dk-old"
            )
            new = await queue.enqueue(
                conn, JobKind.INSTALL, _build_payload(), _AUTHORIZING, "dk-new"
            )
            await conn.execute(
                "UPDATE jobs SET created_at = CASE "
                "WHEN id = %s THEN timestamp '2026-01-01 00:00:00+00' "
                "WHEN id = %s THEN timestamp '2026-01-01 00:00:01+00' "
                "END WHERE id IN (%s, %s)",
                (old.id, new.id, old.id, new.id),
            )
            claimed = await _dequeue(conn, "w1")
            assert claimed is not None
            assert claimed.dedup_key == "dk-old"  # FIFO by created_at
            assert claimed.state is JobState.RUNNING
            assert claimed.worker_id == "w1"
            assert claimed.attempt == 1
            assert claimed.lease_expires_at is not None

    asyncio.run(_run())


def test_dequeue_empty_returns_none(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            assert await _dequeue(conn, "w1") is None

    asyncio.run(_run())


def test_dequeue_concurrent_claims_distinct_jobs(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as setup:
            await queue.enqueue(setup, JobKind.INSTALL, _build_payload(), _AUTHORIZING, "dk-1")
            await queue.enqueue(setup, JobKind.INSTALL, _build_payload(), _AUTHORIZING, "dk-2")
        async with await _connect(migrated_url) as a, await _connect(migrated_url) as b:
            ja, jb = await asyncio.gather(_dequeue(a, "wa"), _dequeue(b, "wb"))
        assert ja is not None and jb is not None
        assert ja.id != jb.id  # SKIP LOCKED: no double-claim

    asyncio.run(_run())


def test_dequeue_skips_future_lease_reclaims_past_lease(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await _insert_running_job(conn, "dk-future", lease_seconds=300)
            assert await _dequeue(conn, "w1") is None  # live lease: not reclaimed

            await _insert_running_job(conn, "dk-past", lease_seconds=-60)
            reclaimed = await _dequeue(conn, "w1")
            assert reclaimed is not None
            assert reclaimed.dedup_key == "dk-past"
            assert reclaimed.worker_id == "w1"
            assert reclaimed.attempt == 1  # 0 -> 1 on reclaim

    asyncio.run(_run())


def test_dequeue_skips_exhausted_attempts(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await _insert_running_job(conn, "dk-done", lease_seconds=-60, attempt=3, max_attempts=3)
            assert await _dequeue(conn, "w1") is None  # attempt == max: left for reconciler

    asyncio.run(_run())


def test_heartbeat_renews_for_owner(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await queue.enqueue(conn, JobKind.INSTALL, _build_payload(), _AUTHORIZING, "dk-hb")
            claimed = await _dequeue(conn, "w1", lease=timedelta(seconds=10))
            assert claimed is not None
            assert claimed.lease_expires_at is not None
            investigation_id, generation = uuid4(), uuid4()
            digest = "f" * 64
            await conn.execute(
                "INSERT INTO investigations (id, principal, project, title, state) "
                "VALUES (%s, 'p', 'proj', 't', 'active')",
                (investigation_id,),
            )
            await conn.execute(
                "INSERT INTO investigation_builds (investigation_id, generation, build_ref, "
                "content_digest, canonical_document, build_result, artifacts, target_kind, "
                "build_profile, expires_at) VALUES "
                "(%s, %s, %s, %s, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, "
                "'local-libvirt', '{}'::jsonb, now() + interval '1 day')",
                (investigation_id, generation, f"{digest}.{generation}", digest),
            )
            use_id = uuid4()
            await conn.execute(
                "INSERT INTO investigation_build_uses (use_id, investigation_id, generation, "
                "job_id, attempt, holder_worker_id, lease_expires_at) "
                "VALUES (%s, %s, %s, %s, %s, 'w1', %s)",
                (
                    use_id,
                    investigation_id,
                    generation,
                    claimed.id,
                    claimed.attempt,
                    claimed.lease_expires_at,
                ),
            )
            assert (
                await queue.heartbeat(
                    conn,
                    claimed.id,
                    attempt=claimed.attempt,
                    incarnation_credential=_credential("w1"),
                    lease=timedelta(minutes=5),
                )
                is True
            )
            cur = await conn.execute(
                "SELECT lease_expires_at FROM jobs WHERE id = %s", (claimed.id,)
            )
            row = await cur.fetchone()
            assert row is not None
            assert row[0] > claimed.lease_expires_at
            use_row = await (
                await conn.execute(
                    "SELECT lease_expires_at FROM investigation_build_uses WHERE use_id = %s",
                    (use_id,),
                )
            ).fetchone()
            assert use_row == row

    asyncio.run(_run())


def test_heartbeat_false_for_non_owner(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await queue.enqueue(conn, JobKind.INSTALL, _build_payload(), _AUTHORIZING, "dk-hb2")
            claimed = await _dequeue(conn, "w1")
            assert claimed is not None
            intruder_credential = await _register_worker(conn, "intruder")
            assert (
                await queue.heartbeat(
                    conn,
                    claimed.id,
                    attempt=claimed.attempt,
                    incarnation_credential=intruder_credential,
                )
                is False
            )

    asyncio.run(_run())


def test_complete_for_owner_and_none_for_non_owner(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await queue.enqueue(conn, JobKind.INSTALL, _build_payload(), _AUTHORIZING, "dk-c1")
            claimed = await _dequeue(conn, "w1")
            assert claimed is not None
            done = await queue.complete(
                conn,
                claimed.id,
                "s3://result",
                attempt=claimed.attempt,
                incarnation_credential=_credential("w1"),
            )
            assert done is not None
            assert done.state is JobState.SUCCEEDED
            assert done.result_ref == "s3://result"

            await queue.enqueue(conn, JobKind.INSTALL, _build_payload(), _AUTHORIZING, "dk-c2")
            other = await _dequeue(conn, "w1")
            assert other is not None
            intruder_credential = await _register_worker(conn, "intruder")
            assert (
                await queue.complete(
                    conn,
                    other.id,
                    "s3://x",
                    attempt=other.attempt,
                    incarnation_credential=intruder_credential,
                )
                is None
            )

    asyncio.run(_run())


def test_fail_requeues_below_max(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await queue.enqueue(
                conn, JobKind.INSTALL, _build_payload(), _AUTHORIZING, "dk-f1", max_attempts=3
            )
            claimed = await _dequeue(conn, "w1")  # attempt -> 1
            assert claimed is not None
            out = await queue.fail(
                conn,
                claimed,
                ErrorCategory.INFRASTRUCTURE_FAILURE,
                incarnation_credential=_credential("w1"),
            )
            assert out.state is JobState.QUEUED
            assert out.worker_id is None
            assert out.lease_expires_at is None

    asyncio.run(_run())


def test_fail_dead_letters_at_max(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            claimed = await _insert_running_job(
                conn, "dk-f2", worker_id="w1", lease_seconds=300, attempt=3, max_attempts=3
            )
            credential = await _register_worker(conn, "w1")
            out = await queue.fail(
                conn,
                claimed,
                ErrorCategory.BUILD_FAILURE,
                incarnation_credential=credential,
            )
            assert out.state is JobState.FAILED
            assert out.error_category is ErrorCategory.BUILD_FAILURE

    asyncio.run(_run())


def test_fail_terminal_dead_letters_below_max(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await queue.enqueue(
                conn, JobKind.INSTALL, _build_payload(), _AUTHORIZING, "dk-f3", max_attempts=3
            )
            claimed = await _dequeue(conn, "w1")  # attempt -> 1, below max
            assert claimed is not None
            out = await queue.fail(
                conn,
                claimed,
                ErrorCategory.NOT_IMPLEMENTED,
                incarnation_credential=_credential("w1"),
                terminal=True,
            )
            assert out.state is JobState.FAILED
            assert out.error_category is ErrorCategory.NOT_IMPLEMENTED

    asyncio.run(_run())


def test_fail_fence_miss_returns_input(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await queue.enqueue(conn, JobKind.INSTALL, _build_payload(), _AUTHORIZING, "dk-f4")
            claimed = await _dequeue(conn, "w1")
            assert claimed is not None
            # Simulate a reclaim by another worker: change worker_id out from under it.
            await _register_worker(conn, "w2")
            await conn.execute("UPDATE jobs SET worker_id = 'w2' WHERE id = %s", (claimed.id,))
            out = await queue.fail(
                conn,
                claimed,
                ErrorCategory.INFRASTRUCTURE_FAILURE,
                incarnation_credential=_credential("w1"),
            )
            assert out is claimed  # fence missed: unchanged input returned

    asyncio.run(_run())


def test_recent_jobs_newest_first_and_capped(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            for i in range(3):
                await queue.enqueue(
                    conn,
                    JobKind.INSTALL,
                    _build_payload(),
                    cast(Any, {"principal": "p", "project": "proj"}),
                    f"d{i}",
                )
            recent = await queue.recent_jobs(conn, limit=2, projects=["proj"])
        assert len(recent) == 2
        # newest-first: the last-enqueued dedup_key appears first
        assert recent[0].dedup_key == "d2"
        assert recent[1].dedup_key == "d1"

    asyncio.run(_run())


def test_recent_jobs_empty(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            assert await queue.recent_jobs(conn, limit=10, projects=["proj"]) == []

    asyncio.run(_run())


def test_recent_jobs_filters_by_project(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await queue.enqueue(conn, JobKind.INSTALL, _build_payload(), _AUTHORIZING, "ja")
            await queue.enqueue(
                conn,
                JobKind.INSTALL,
                _build_payload(),
                Authorizing(principal="p", project="b"),
                "jb",
            )
            await conn.execute(
                "INSERT INTO jobs (kind, payload, state, max_attempts, authorizing, dedup_key) "
                "VALUES (%s, %s, 'queued', 3, %s, 'jnone')",
                (
                    JobKind.INSTALL.value,
                    Jsonb(_build_payload().model_dump(mode="json", exclude_none=True)),
                    Jsonb({"principal": "p"}),
                ),
            )
            recent = await queue.recent_jobs(conn, limit=10, projects=["a"])
        assert [j.dedup_key for j in recent] == ["ja"]  # only project a; b and no-project excluded

    asyncio.run(_run())


def test_recent_jobs_empty_projects_returns_nothing(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await queue.enqueue(conn, JobKind.INSTALL, _build_payload(), _AUTHORIZING, "ja")
            assert await queue.recent_jobs(conn, limit=10, projects=[]) == []

    asyncio.run(_run())


async def _enqueue_with_state(
    conn: psycopg.AsyncConnection,
    kind: JobKind,
    state: JobState,
    dedup: str,
    *,
    project: str = "proj",
    payload: dict[str, Any] | None = None,
) -> None:
    """Insert a job in an explicit ``kind``/``state`` for ``project`` (filter-test seeding)."""
    await conn.execute(
        "INSERT INTO jobs (kind, payload, state, max_attempts, authorizing, dedup_key) "
        "VALUES (%s, %s, %s, 3, %s, %s)",
        (
            kind.value,
            Jsonb(payload if payload is not None else _build_payload().model_dump(mode="json")),
            state.value,
            Jsonb({"principal": "p", "agent_session": None, "project": project}),
            dedup,
        ),
    )


async def _seed_run_in_investigation(conn: psycopg.AsyncConnection, project: str = "proj") -> str:
    """Insert a minimal Investigation + Run; return the Run id for a job payload's ``run_id``.

    ``system_id`` is nullable since ADR-0169, so the Run needs only its Investigation FK,
    a committed ``target_kind``, state, profile, and ownership — enough for the
    ``jobs.payload->>'run_id' -> runs.investigation_id`` filter join under test.
    """
    cur = await conn.execute(
        "INSERT INTO investigations (title, state, principal, project) "
        "VALUES ('inv', 'active', 'p', %s) RETURNING id",
        (project,),
    )
    inv_row = await cur.fetchone()
    assert inv_row is not None
    cur = await conn.execute(
        "INSERT INTO runs (investigation_id, state, build_profile, principal, project, "
        "target_kind) VALUES (%s, 'running', '{}'::jsonb, 'p', %s, 'local_libvirt') "
        "RETURNING id",
        (inv_row[0], project),
    )
    run_row = await cur.fetchone()
    assert run_row is not None
    return str(run_row[0])


def test_recent_jobs_filters_by_status(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await _enqueue_with_state(conn, JobKind.INSTALL, JobState.FAILED, "f1")
            await _enqueue_with_state(conn, JobKind.INSTALL, JobState.QUEUED, "q1")
            recent = await queue.recent_jobs(
                conn, limit=10, projects=["proj"], status=JobState.FAILED
            )
        assert [j.dedup_key for j in recent] == ["f1"]

    asyncio.run(_run())


def test_recent_jobs_filters_by_kind(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await _enqueue_with_state(conn, JobKind.INSTALL, JobState.QUEUED, "b1")
            await _enqueue_with_state(
                conn,
                JobKind.PROVISION,
                JobState.QUEUED,
                "p1",
                payload=_system_payload().model_dump(mode="json"),
            )
            recent = await queue.recent_jobs(
                conn, limit=10, projects=["proj"], kind=JobKind.INSTALL
            )
        assert [j.dedup_key for j in recent] == ["b1"]

    asyncio.run(_run())


def test_recent_jobs_filters_by_status_and_kind_conjunction(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await _enqueue_with_state(conn, JobKind.INSTALL, JobState.FAILED, "fb")
            await _enqueue_with_state(conn, JobKind.INSTALL, JobState.QUEUED, "qb")
            await _enqueue_with_state(
                conn,
                JobKind.PROVISION,
                JobState.FAILED,
                "fp",
                payload=_system_payload().model_dump(mode="json"),
            )
            recent = await queue.recent_jobs(
                conn, limit=10, projects=["proj"], status=JobState.FAILED, kind=JobKind.INSTALL
            )
        assert [j.dedup_key for j in recent] == ["fb"]

    asyncio.run(_run())


def test_recent_jobs_filters_by_investigation_id(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            run_id = await _seed_run_in_investigation(conn)
            inv_row = await (
                await conn.execute("SELECT investigation_id FROM runs WHERE id = %s", (run_id,))
            ).fetchone()
            assert inv_row is not None
            investigation_id = inv_row[0]
            # An install job whose payload points at the investigation's run.
            await _enqueue_with_state(
                conn,
                JobKind.INSTALL,
                JobState.QUEUED,
                "in-inv",
                payload=_install_payload(run_id).model_dump(mode="json", exclude_none=True),
            )
            # An install job for an unrelated run, and a run-less provision job: both excluded.
            await _enqueue_with_state(conn, JobKind.INSTALL, JobState.QUEUED, "other-run")
            await _enqueue_with_state(
                conn,
                JobKind.PROVISION,
                JobState.QUEUED,
                "no-run",
                payload=_system_payload().model_dump(mode="json"),
            )
            recent = await queue.recent_jobs(
                conn, limit=10, projects=["proj"], investigation_id=investigation_id
            )
        assert [j.dedup_key for j in recent] == ["in-inv"]

    asyncio.run(_run())


def test_recent_jobs_investigation_filter_no_match_is_empty(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await _enqueue_with_state(conn, JobKind.INSTALL, JobState.QUEUED, "b1")
            recent = await queue.recent_jobs(
                conn, limit=10, projects=["proj"], investigation_id=uuid4()
            )
        assert recent == []

    asyncio.run(_run())


def test_recent_jobs_investigation_filter_excludes_other_project(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            run_id = await _seed_run_in_investigation(conn, project="other")
            inv_row = await (
                await conn.execute("SELECT investigation_id FROM runs WHERE id = %s", (run_id,))
            ).fetchone()
            assert inv_row is not None
            # The job is owned by 'other' but the caller can only read 'proj': the project
            # predicate excludes it even though the investigation id matches (no leak).
            await _enqueue_with_state(
                conn,
                JobKind.INSTALL,
                JobState.QUEUED,
                "other-proj",
                project="other",
                payload=_install_payload(run_id).model_dump(mode="json", exclude_none=True),
            )
            recent = await queue.recent_jobs(
                conn, limit=10, projects=["proj"], investigation_id=inv_row[0]
            )
        assert recent == []

    asyncio.run(_run())


def test_recent_jobs_filters_by_system_id(migrated_url: str) -> None:
    async def _run() -> None:
        target = uuid4()
        async with await _connect(migrated_url) as conn:
            await _enqueue_with_state(
                conn,
                JobKind.AUTHORIZE_SSH_KEY,
                JobState.SUCCEEDED,
                "mine",
                payload=AuthorizeSshKeyPayload(
                    system_id=str(target), public_key="ssh-ed25519 AAAA"
                ).model_dump(mode="json"),
            )
            await _enqueue_with_state(
                conn,
                JobKind.AUTHORIZE_SSH_KEY,
                JobState.SUCCEEDED,
                "other-system",
                payload=SystemPayload(system_id=str(uuid4())).model_dump(mode="json"),
            )
            recent = await queue.recent_jobs(conn, limit=10, projects=["proj"], system_id=target)
        assert [j.dedup_key for j in recent] == ["mine"]

    asyncio.run(_run())


def test_recent_jobs_system_scoped_job_excluded_from_investigation_filter(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        target = uuid4()
        async with await _connect(migrated_url) as conn:
            run_id = await _seed_run_in_investigation(conn)
            inv_row = await (
                await conn.execute("SELECT investigation_id FROM runs WHERE id = %s", (run_id,))
            ).fetchone()
            assert inv_row is not None
            # A check_ssh_reachable job carries system_id but no run_id.
            await _enqueue_with_state(
                conn,
                JobKind.CHECK_SSH_REACHABLE,
                JobState.SUCCEEDED,
                "ssh-probe",
                payload=SystemPayload(system_id=str(target)).model_dump(mode="json"),
            )
            by_system = await queue.recent_jobs(conn, limit=10, projects=["proj"], system_id=target)
            by_investigation = await queue.recent_jobs(
                conn, limit=10, projects=["proj"], investigation_id=inv_row[0]
            )
        assert [j.dedup_key for j in by_system] == ["ssh-probe"]
        assert [j.dedup_key for j in by_investigation] == []

    asyncio.run(_run())


def test_queue_paused_defaults_false_and_toggles(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            assert await queue.is_queue_paused(conn) is False  # seeded row, default false
            await queue.set_queue_paused(conn, True)
            assert await queue.is_queue_paused(conn) is True
            await queue.set_queue_paused(conn, False)
            assert await queue.is_queue_paused(conn) is False

    asyncio.run(_run())


def test_ops_control_is_single_row(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            cur = await conn.execute("SELECT count(*) FROM ops_control")
            row = await cur.fetchone()
            assert row is not None and row[0] == 1  # seeded exactly once
            with pytest.raises(psycopg.errors.UniqueViolation):
                await conn.execute("INSERT INTO ops_control (singleton) VALUES (true)")

    asyncio.run(_run())


def test_is_queue_paused_fails_closed_when_row_missing(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await conn.execute("DELETE FROM ops_control")
            assert await queue.is_queue_paused(conn) is True  # missing row → fail closed (paused)

    asyncio.run(_run())
