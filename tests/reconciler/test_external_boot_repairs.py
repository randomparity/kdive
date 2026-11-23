"""Post-prepared external-boot reconciler repair contracts (#2203)."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Iterator
from typing import Any, Literal, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.errors import ForeignKeyViolation, InsufficientPrivilege, QueryCanceled
from psycopg.pq import TransactionStatus
from psycopg.sql import SQL
from psycopg.sql import Literal as SqlLiteral
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.jobs.payloads import BootPayload
from kdive.providers.infra.reaping import NullReaper
from kdive.reconciler import loop
from kdive.reconciler.repairs import external_boot
from tests.db.external_boot_authority_support import _RoleDsns
from tests.jobs.handlers.external_boot.conftest import resolver_for
from tests.jobs.handlers.external_boot.seeding import SeededCase, seed_case
from tests.jobs.handlers.external_boot.support import marked_job
from tests.jobs.handlers.external_boot.vehicle import Vehicle, build_vehicle
from tests.reconcile_helpers import make_reconcile_config


@pytest.mark.parametrize(
    ("lane", "predicate"),
    [
        ("activation", "activation_readiness_deadline < now()"),
        ("recovery", "recovery_readiness_deadline < now()"),
        ("release", "reservation.state = 'ready'"),
        ("cleanup", "NOT a.cleanup_complete"),
    ],
)
def test_candidate_lanes_use_durable_state(lane: external_boot.RepairLane, predicate: str) -> None:
    assert predicate in external_boot._CANDIDATE_SQL[lane]
    assert "preparing" not in external_boot._CANDIDATE_SQL[lane]


def test_repair_rebinds_successor_to_current_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    activation_id = uuid4()
    source = marked_job(
        "activate", activation_id=str(activation_id), authority_instance="authority-old"
    )
    payload = BootPayload.model_validate(source.payload)
    marker = payload.external_boot_authority_v1
    assert marker is not None
    candidate = external_boot._Candidate(
        activation_id,
        marker.system_id,
        marker.run_id,
        marker.plan_identity,
        source.authorizing["project"],
        "activate",
    )
    build = AsyncMock(return_value=(source.kind, payload))
    enqueue = AsyncMock(return_value=(source, True))
    monkeypatch.setattr(external_boot, "build_external_boot_payload", build)
    monkeypatch.setattr(external_boot.queue, "enqueue_with_status", enqueue)

    repaired = asyncio.run(
        external_boot._enqueue_candidate(
            cast(Any, object()),
            candidate,
            resolver=cast(Any, object()),
            authority_instance="authority-current",
            source_jobs=(source,),
            live_jobs=(),
        )
    )

    assert repaired is True
    call = build.await_args
    assert call is not None
    assert call.kwargs["authority_instance"] == "authority-current"
    assert str(source.id) in call.kwargs["operation_identity"]
    assert enqueue.await_count == 1


def test_live_successor_suppresses_enqueue(monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = external_boot._Candidate(
        uuid4(), uuid4(), uuid4(), "sha256:" + "a" * 64, "kernel-team", "cleanup"
    )
    source = marked_job(
        "cleanup",
        activation_id=candidate.activation_id,
        system_id=candidate.system_id,
        run_id=candidate.run_id,
        plan_identity=candidate.plan_identity,
        authority_instance="authority-current",
    )

    repaired = asyncio.run(
        external_boot._enqueue_candidate(
            cast(Any, object()),
            candidate,
            resolver=cast(Any, object()),
            authority_instance="authority-current",
            source_jobs=(source,),
            live_jobs=(source,),
        )
    )

    assert repaired is False


def test_job_lookup_is_set_oriented_and_bounded() -> None:
    source = inspect.getsource(external_boot._candidate_jobs)
    assert "row_number() OVER" in source
    assert "candidate_rank <= %s" in source
    assert "statement_timeout" in source
    assert "PARTITION BY candidate.activation_id" in source
    assert "system_id}'" in source
    assert "authorizing ->> 'project'" in source
    repair = inspect.getsource(external_boot.repair_external_boot_lane)
    assert repair.count("_candidate_jobs(") == 2


def test_commit_failure_is_not_counted_and_next_candidate_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = tuple(
        external_boot._Candidate(
            uuid4(), uuid4(), uuid4(), "sha256:" + "a" * 64, "kernel-team", "activate"
        )
        for _index in range(2)
    )

    class Transaction:
        def __init__(self, fail_commit: bool) -> None:
            self.fail_commit = fail_commit

        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_args: object) -> None:
            if self.fail_commit:
                raise RuntimeError("controlled commit failure")

    class Connection:
        transactions = 0

        def transaction(self) -> Transaction:
            self.transactions += 1
            return Transaction(fail_commit=self.transactions == 1)

    enqueue = AsyncMock(return_value=True)
    monkeypatch.setattr(external_boot, "_candidates", AsyncMock(return_value=candidates))
    monkeypatch.setattr(external_boot, "_candidate_jobs", AsyncMock(return_value=()))
    monkeypatch.setattr(external_boot, "_enqueue_candidate", enqueue)

    repaired = asyncio.run(
        external_boot.repair_external_boot_lane(
            cast(Any, Connection()),
            lane="activation",
            resolver=cast(Any, object()),
            authority_instance="authority-current",
        )
    )

    assert repaired == 1
    assert enqueue.await_count == 2


def test_cleanup_uses_release_purpose() -> None:
    assert external_boot._purpose("cleanup") == "release"
    assert external_boot._purpose("teardown") == "teardown"


def test_repair_module_has_no_provider_adapter_imports() -> None:
    source = inspect.getsource(external_boot)
    assert "providers.local_libvirt" not in source
    assert "providers.remote_libvirt" not in source
    assert "import libvirt" not in source


def test_reconciler_role_enqueues_one_exhausted_prepared_successor(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body() -> None:
        vehicle = build_vehicle()
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as admin:
            case = await seed_case(
                admin,
                vehicle,
                purpose="activate",
                activation_state="prepared",
            )
            await admin.execute(
                "UPDATE jobs SET attempt = max_attempts, lease_expires_at = now() - interval '1s' "
                "WHERE id = %s",
                (case.job_id,),
            )
            await admin.execute(
                "INSERT INTO jobs (kind, payload, state, max_attempts, authorizing, dedup_key) "
                "SELECT kind, jsonb_set(payload, "
                "'{external_boot_authority_v1,authority_instance}', %s), "
                "'queued', max_attempts, jsonb_set(authorizing, '{project}', %s), "
                "'foreign-suppressor' FROM jobs WHERE id = %s",
                (Jsonb("authority-current"), Jsonb("other"), case.job_id),
            )
            await admin.execute(
                "INSERT INTO jobs (kind, payload, state, max_attempts, authorizing, dedup_key) "
                "SELECT kind, jsonb_set(payload, '{external_boot_authority_v1,run_id}', %s), "
                "'failed', max_attempts, authorizing, 'quota-poison-' || n "
                "FROM jobs CROSS JOIN generate_series(1, 100) AS n WHERE id = %s",
                (Jsonb(str(uuid4())), case.job_id),
            )
        async with await psycopg.AsyncConnection.connect(
            authority_role_dsns("kdive_reconciler")
        ) as reconciler:
            first = await external_boot.repair_external_boot_lane(
                reconciler,
                lane="activation",
                resolver=resolver_for(vehicle),
                authority_instance="authority-current",
            )
            second = await external_boot.repair_external_boot_lane(
                reconciler,
                lane="activation",
                resolver=resolver_for(vehicle),
                authority_instance="authority-current",
            )
            assert (first, second) == (1, 0)
            assert reconciler.info.transaction_status is TransactionStatus.IDLE
            row = await reconciler.execute(
                "SELECT payload #>> '{external_boot_authority_v1,authority_instance}', state "
                "FROM jobs WHERE dedup_key LIKE 'external-boot:repair:%'"
            )
            successor = await row.fetchone()
            assert successor == ("authority-current", "queued")
            with pytest.raises(InsufficientPrivilege):
                await reconciler.execute(
                    "UPDATE external_boot_activations SET cleanup_complete = true WHERE id = %s",
                    (vehicle.activation_id,),
                )

    asyncio.run(body())


def test_job_lookup_timeout_ends_its_transaction(
    migrated_url: str, authority_role_dsns: _RoleDsns, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def body() -> None:
        vehicle = build_vehicle()
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as admin:
            await seed_case(admin, vehicle, purpose="activate", activation_state="prepared")
        async with (
            await psycopg.AsyncConnection.connect(migrated_url) as blocker,
            await psycopg.AsyncConnection.connect(
                authority_role_dsns("kdive_reconciler")
            ) as reconciler,
        ):
            await blocker.execute("LOCK TABLE jobs IN ACCESS EXCLUSIVE MODE")
            monkeypatch.setattr(external_boot, "_JOB_LOOKUP_TIMEOUT_MS", 10)
            cursors: external_boot.RepairCursors = {"activation": UUID(int=0)}
            with pytest.raises(QueryCanceled):
                await external_boot.repair_external_boot_lane(
                    reconciler,
                    lane="activation",
                    resolver=resolver_for(vehicle),
                    authority_instance="authority-current",
                    cursors=cursors,
                )
            assert reconciler.info.transaction_status is TransactionStatus.IDLE
            assert cursors == {"activation": UUID(int=0)}

    asyncio.run(body())


@pytest.mark.parametrize(
    ("lane", "purpose", "activation_state", "seed_options", "expected_operation"),
    [
        ("recovery", "recover", "recovering", {}, "recover"),
        (
            "recovery",
            "resolve-conflict",
            "recovering",
            {"with_pre_recovery": True},
            "resolve-conflict",
        ),
        ("release", "release", "recovered", {"with_reservation": True}, "release"),
        ("cleanup", "release", "recovered", {"with_release": True}, "cleanup"),
        (
            "cleanup",
            "teardown",
            "recovery_failed",
            {"attempt_state": "failed", "with_release": True},
            "teardown",
        ),
    ],
)
def test_each_post_prepared_lane_enqueues_its_existing_worker_operation(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    lane: str,
    purpose: str,
    activation_state: str,
    seed_options: dict[str, Any],
    expected_operation: str,
) -> None:
    async def body() -> None:
        vehicle = build_vehicle()
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as admin:
            case = await seed_case(
                admin,
                vehicle,
                purpose=purpose,
                operation=expected_operation,
                activation_state=activation_state,
                **seed_options,
            )
            await admin.execute(
                "UPDATE jobs SET attempt = max_attempts, lease_expires_at = now() - interval '1s' "
                "WHERE id = %s",
                (case.job_id,),
            )
            if lane == "recovery":
                await admin.execute(
                    "UPDATE external_boot_recovery_attempts "
                    "SET recovery_readiness_deadline = now() - interval '1s', "
                    "recovery_basis = %s "
                    "WHERE activation_id = %s",
                    (
                        "pre_recovery"
                        if expected_operation == "resolve-conflict"
                        else "recovery_point",
                        vehicle.activation_id,
                    ),
                )
        async with await psycopg.AsyncConnection.connect(
            authority_role_dsns("kdive_reconciler"), autocommit=True
        ) as reconciler:
            count = await external_boot.repair_external_boot_lane(
                reconciler,
                lane=cast(Any, lane),
                resolver=resolver_for(vehicle),
                authority_instance="authority-current",
            )
            assert count == 1
            cursor = await reconciler.execute(
                "SELECT payload #>> '{external_boot_authority_v1,operation}' "
                "FROM jobs WHERE dedup_key LIKE 'external-boot:repair:%'"
            )
            row = await cursor.fetchone()
            assert row == (expected_operation,)

    asyncio.run(body())


@pytest.mark.parametrize("activation_state", ["recovery_failed", "recovery_conflict", "abandoned"])
def test_release_lane_skips_states_the_release_handler_refuses(
    migrated_url: str, authority_role_dsns: _RoleDsns, activation_state: str
) -> None:
    async def body() -> None:
        vehicle = build_vehicle()
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as admin:
            await seed_case(
                admin,
                vehicle,
                purpose="release",
                activation_state=activation_state,
                attempt_state="failed",
                with_reservation=True,
            )
        async with await psycopg.AsyncConnection.connect(
            authority_role_dsns("kdive_reconciler"), autocommit=True
        ) as reconciler:
            for _ in range(2):
                assert (
                    await external_boot.repair_external_boot_lane(
                        reconciler,
                        lane="release",
                        resolver=resolver_for(vehicle),
                        authority_instance="authority-current",
                    )
                    == 0
                )
            cursor = await reconciler.execute(
                "SELECT count(*) FROM jobs WHERE dedup_key LIKE 'external-boot:repair:%'"
            )
            assert await cursor.fetchone() == (0,)

    asyncio.run(body())


def test_reservation_cannot_exist_without_an_activation(migrated_url: str) -> None:
    async def body() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            with pytest.raises(ForeignKeyViolation):
                await conn.execute(
                    "INSERT INTO external_boot_reservations "
                    "(activation_id, store_identity, owner_key, reserved_bytes, state) "
                    "VALUES (%s, 'store', 'owner', 1, 'pending')",
                    (uuid4(),),
                )

    asyncio.run(body())


@pytest.mark.parametrize("failure_kind", ["database", "payload", "provider"])
def test_repair_failures_keep_safe_diagnostics_and_transaction_isolation(
    migrated_url: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_kind: str,
) -> None:
    # Synthetic marker verifies exception details never reach logs.
    secret = "sentinel-provider-secret"  # pragma: allowlist secret
    failures = {
        "database": QueryCanceled(secret),
        "payload": ValueError(secret),
        "provider": CategorizedError(secret, category=ErrorCategory.TRANSPORT_FAILURE),
    }
    failure = failures[failure_kind]
    candidates = tuple(
        external_boot._Candidate(
            uuid4(), uuid4(), uuid4(), "sha256:" + "a" * 64, secret, "activate"
        )
        for _ in range(2)
    )
    monkeypatch.setattr(external_boot, "_candidates", AsyncMock(return_value=candidates))
    monkeypatch.setattr(external_boot, "_candidate_jobs", AsyncMock(return_value=()))

    async def enqueue(
        conn: psycopg.AsyncConnection, candidate: external_boot._Candidate, **_kwargs: object
    ) -> bool:
        await conn.execute(
            "INSERT INTO repair_attempt_probe (candidate_id) VALUES (%s)",
            (candidate.activation_id,),
        )
        if candidate is candidates[0]:
            raise failure
        return True

    monkeypatch.setattr(external_boot, "_enqueue_candidate", enqueue)

    async def body() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            await conn.execute("CREATE TEMP TABLE repair_attempt_probe (candidate_id uuid)")
            for _ in range(2):
                assert (
                    await external_boot.repair_external_boot_lane(
                        conn,
                        lane="activation",
                        resolver=resolver_for(build_vehicle()),
                        authority_instance=secret,
                    )
                    == 1
                )
                assert conn.info.transaction_status is TransactionStatus.IDLE
            cursor = await conn.execute("SELECT candidate_id FROM repair_attempt_probe")
            assert await cursor.fetchall() == [(candidates[1].activation_id,)] * 2

    asyncio.run(body())
    records = [r for r in caplog.records if r.name == external_boot.__name__]
    assert len(records) == 2
    expected_category = (
        ErrorCategory.TRANSPORT_FAILURE
        if failure_kind == "provider"
        else ErrorCategory.INFRASTRUCTURE_FAILURE
    )
    for record in records:
        message = record.getMessage()
        assert f"activation_id={candidates[0].activation_id}" in message
        assert f"exception={type(failure).__name__}" in message
        assert f"category={expected_category.value}" in message
        assert secret not in message
        assert record.exc_info is None


async def _seed_repair_page(
    admin: psycopg.AsyncConnection,
    *,
    lane: external_boot.RepairLane,
    blocking: Literal["missing-source", "live", "enqueue-error"],
    first_id: int = 0,
) -> tuple[list[Vehicle], list[SeededCase]]:
    purpose, state, operation = {
        "activation": ("activate", "prepared", "activate"),
        "recovery": ("recover", "recovering", "recover"),
        "release": ("release", "recovered", "release"),
        "cleanup": ("release", "recovered", "cleanup"),
    }[lane]
    vehicles = [build_vehicle(activation_id=UUID(int=first_id + i)) for i in range(101)]
    cases = []
    for index, vehicle in enumerate(vehicles):
        case = await seed_case(
            admin,
            vehicle,
            purpose=purpose,
            operation=operation,
            activation_state=state,
            marker_overrides={"authority_instance": "authority-current"},
            with_reservation=lane == "release",
            with_release=lane == "cleanup",
        )
        cases.append(case)
        if lane == "recovery":
            await admin.execute(
                "UPDATE external_boot_recovery_attempts SET recovery_readiness_deadline="
                "clock_timestamp()-interval '1 second' WHERE activation_id=%s",
                (vehicle.activation_id,),
            )
        if index < 100 and blocking == "missing-source":
            await admin.execute("DELETE FROM jobs WHERE id=%s", (case.job_id,))
        elif index == 100 or blocking == "enqueue-error":
            await admin.execute(
                "UPDATE jobs SET attempt=max_attempts,lease_expires_at="
                "clock_timestamp()-interval '1 second' WHERE id=%s",
                (case.job_id,),
            )
    return vehicles, cases


def _lane_entry(lane: external_boot.RepairLane) -> loop._RepairCatalogEntry:
    plural = {
        "activation": "activations",
        "recovery": "recoveries",
        "release": "releases",
        "cleanup": "cleanups",
    }[lane]
    return next(
        entry for entry in loop._REPAIR_CATALOG if entry.name == f"external_boot_{plural}_enqueued"
    )


def _runner(pool: AsyncConnectionPool, vehicle: Vehicle) -> loop.Reconciler:
    return loop.Reconciler(
        pool,
        NullReaper(),
        config=make_reconcile_config(
            provider_resolver=resolver_for(vehicle),
            external_boot_authority_instance="authority-current",
        ),
    )


async def _lane_count(runner: loop.Reconciler, entry: loop._RepairCatalogEntry) -> int:
    report = await runner.run_once()
    assert report.failures == ()
    assert entry.report_field is not None
    count = getattr(report, entry.report_field)
    assert isinstance(count, int)
    return count


@pytest.mark.parametrize("lane", ["activation", "recovery", "release", "cleanup"])
@pytest.mark.parametrize("blocking", ["missing-source", "live"])
def test_reconciler_advances_beyond_a_full_skipped_page(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    monkeypatch: pytest.MonkeyPatch,
    lane: external_boot.RepairLane,
    blocking: Literal["missing-source", "live"],
) -> None:
    entry = _lane_entry(lane)
    monkeypatch.setattr(loop, "_REPAIR_CATALOG", (entry,))

    async def body() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as admin:
            vehicles, _ = await _seed_repair_page(admin, lane=lane, blocking=blocking)
        async with AsyncConnectionPool(
            authority_role_dsns("kdive_reconciler"), min_size=1, max_size=2, open=False
        ) as pool:
            runner = _runner(pool, vehicles[-1])
            assert await _lane_count(runner, entry) == 0
            assert await _lane_count(runner, entry) == 1, "a skipped first page starved later work"
            async with pool.connection() as conn:
                rows = await (
                    await conn.execute(
                        "SELECT payload #>> '{external_boot_authority_v1,activation_id}' "
                        "FROM jobs WHERE dedup_key LIKE 'external-boot:repair:%'"
                    )
                ).fetchall()
                assert rows == [(str(vehicles[-1].activation_id),)]

    asyncio.run(body())


def test_reconcilers_sharing_config_keep_independent_progress(
    migrated_url: str, authority_role_dsns: _RoleDsns, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _lane_entry("activation")
    monkeypatch.setattr(loop, "_REPAIR_CATALOG", (entry,))

    async def body() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as admin:
            vehicles, _ = await _seed_repair_page(admin, lane="activation", blocking="live")
        async with AsyncConnectionPool(
            authority_role_dsns("kdive_reconciler"), min_size=1, max_size=2, open=False
        ) as pool:
            config = make_reconcile_config(
                provider_resolver=resolver_for(vehicles[-1]),
                external_boot_authority_instance="authority-current",
            )
            first = loop.Reconciler(pool, NullReaper(), config=config)
            peer = loop.Reconciler(pool, NullReaper(), config=config)
            assert await _lane_count(first, entry) == 0
            assert await _lane_count(peer, entry) == 0, "another instance inherited this cursor"
            assert await _lane_count(first, entry) == 1
            assert await _lane_count(peer, entry) == 0  # The durable live successor now owns it.

    asyncio.run(body())


def test_wrap_revisits_a_previously_live_candidate(
    migrated_url: str, authority_role_dsns: _RoleDsns, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _lane_entry("activation")
    monkeypatch.setattr(loop, "_REPAIR_CATALOG", (entry,))

    async def body() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as admin:
            vehicles, cases = await _seed_repair_page(admin, lane="activation", blocking="live")
        async with AsyncConnectionPool(
            authority_role_dsns("kdive_reconciler"), min_size=1, max_size=2, open=False
        ) as pool:
            runner = _runner(pool, vehicles[-1])
            assert await _lane_count(runner, entry) == 0
            assert await _lane_count(runner, entry) == 1
            async with await psycopg.AsyncConnection.connect(
                migrated_url, autocommit=True
            ) as admin:
                await admin.execute(
                    "UPDATE jobs SET attempt=max_attempts,lease_expires_at="
                    "clock_timestamp()-interval '1 second' WHERE id=%s",
                    (cases[0].job_id,),
                )
            assert await _lane_count(runner, entry) == 1, "the cursor never wrapped to earlier work"
            assert await _lane_count(runner, entry) == 0
            async with pool.connection() as conn:
                rows = await (
                    await conn.execute(
                        "SELECT payload #>> '{external_boot_authority_v1,activation_id}' "
                        "FROM jobs WHERE dedup_key LIKE 'external-boot:repair:%'"
                    )
                ).fetchall()
                assert {row[0] for row in rows} == {
                    str(vehicles[0].activation_id),
                    str(vehicles[-1].activation_id),
                }

    asyncio.run(body())


def test_lanes_advance_independently_when_interleaved(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as admin:
            activation, _ = await _seed_repair_page(
                admin, lane="activation", blocking="missing-source", first_id=1000
            )
            recovery, _ = await _seed_repair_page(admin, lane="recovery", blocking="missing-source")
        cursors: external_boot.RepairCursors = {}
        async with await psycopg.AsyncConnection.connect(
            authority_role_dsns("kdive_reconciler"), autocommit=True
        ) as conn:

            async def repair(lane: external_boot.RepairLane, vehicle: Vehicle) -> int:
                return await external_boot.repair_external_boot_lane(
                    conn,
                    lane=lane,
                    resolver=resolver_for(vehicle),
                    authority_instance="authority-current",
                    cursors=cursors,
                )

            assert await repair("activation", activation[-1]) == 0
            assert await repair("recovery", recovery[-1]) == 0
            assert await repair("activation", activation[-1]) == 1, "another lane reset this cursor"
            assert await repair("recovery", recovery[-1]) == 1

    asyncio.run(body())


type _InsertFault = Callable[[Literal["reject", "block"], UUID], None]


@pytest.fixture
def repair_insert_fault(migrated_url: str) -> Iterator[_InsertFault]:
    with psycopg.connect(migrated_url) as admin:
        admin.execute("""
            CREATE FUNCTION test_repair_insert_fault() RETURNS trigger
            LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
            DECLARE v_id uuid;
            BEGIN
                IF NEW.dedup_key NOT LIKE 'external-boot:repair:%' THEN RETURN NEW; END IF;
                v_id := (NEW.payload #>> '{external_boot_authority_v1,activation_id}')::uuid;
                IF TG_ARGV[0]='reject' AND v_id<TG_ARGV[1]::uuid THEN
                    RAISE EXCEPTION 'injected repair enqueue failure';
                ELSIF TG_ARGV[0]='block' AND v_id=TG_ARGV[1]::uuid THEN
                    PERFORM pg_advisory_xact_lock(hashtextextended(
                        'test:external-boot-repair:' || TG_ARGV[1],0));
                END IF;
                RETURN NEW;
            END $$
        """)

    def install(mode: Literal["reject", "block"], activation_id: UUID) -> None:
        with psycopg.connect(migrated_url) as admin:
            admin.execute(
                SQL(
                    "CREATE TRIGGER test_repair_insert_fault BEFORE INSERT ON jobs "
                    "FOR EACH ROW EXECUTE FUNCTION test_repair_insert_fault({},{})"
                ).format(SqlLiteral(mode), SqlLiteral(str(activation_id)))
            )

    try:
        yield install
    finally:
        with psycopg.connect(migrated_url) as admin:
            admin.execute("DROP TRIGGER IF EXISTS test_repair_insert_fault ON jobs")
            admin.execute("DROP FUNCTION test_repair_insert_fault()")


def test_enqueue_failures_advance_beyond_a_full_page(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    monkeypatch: pytest.MonkeyPatch,
    repair_insert_fault: _InsertFault,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = _lane_entry("activation")
    monkeypatch.setattr(loop, "_REPAIR_CATALOG", (entry,))

    async def body() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as admin:
            vehicles, _ = await _seed_repair_page(
                admin, lane="activation", blocking="enqueue-error"
            )
        repair_insert_fault("reject", vehicles[-1].activation_id)
        async with AsyncConnectionPool(
            authority_role_dsns("kdive_reconciler"), min_size=1, max_size=2, open=False
        ) as pool:
            runner = _runner(pool, vehicles[-1])
            assert await _lane_count(runner, entry) == 0
            assert (
                sum("candidate repair failed" in row.getMessage() for row in caplog.records) == 100
            )
            assert await _lane_count(runner, entry) == 1
        assert all(
            "injected repair enqueue failure" not in row.getMessage() for row in caplog.records
        )

    asyncio.run(body())


def test_cancelled_insert_keeps_the_interrupted_candidate_for_retry(
    migrated_url: str, authority_role_dsns: _RoleDsns, repair_insert_fault: _InsertFault
) -> None:
    async def body() -> None:
        vehicles = [build_vehicle(activation_id=UUID(int=i)) for i in range(3)]
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as admin:
            for vehicle in vehicles:
                case = await seed_case(
                    admin,
                    vehicle,
                    purpose="activate",
                    activation_state="prepared",
                    marker_overrides={"authority_instance": "authority-current"},
                )
                await admin.execute(
                    "UPDATE jobs SET attempt=max_attempts,lease_expires_at="
                    "clock_timestamp()-interval '1 second' WHERE id=%s",
                    (case.job_id,),
                )
        repair_insert_fault("block", vehicles[1].activation_id)
        cursors: external_boot.RepairCursors = {}
        async with (
            await psycopg.AsyncConnection.connect(migrated_url) as blocker,
            await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as observer,
            await psycopg.AsyncConnection.connect(
                authority_role_dsns("kdive_reconciler"), autocommit=True
            ) as conn,
        ):
            await blocker.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended("
                "'test:external-boot-repair:' || %s,0))",
                (str(vehicles[1].activation_id),),
            )

            async def repair() -> int:
                return await external_boot.repair_external_boot_lane(
                    conn,
                    lane="activation",
                    resolver=resolver_for(vehicles[-1]),
                    authority_instance="authority-current",
                    cursors=cursors,
                )

            task = asyncio.create_task(repair())
            try:
                deadline = asyncio.get_running_loop().time() + 5
                while (
                    await (
                        await observer.execute(
                            "SELECT 1 FROM pg_locks WHERE pid=%s AND locktype='advisory' "
                            "AND NOT granted",
                            (conn.info.backend_pid,),
                        )
                    ).fetchone()
                    is None
                ):
                    if task.done():
                        await task
                        raise AssertionError("the second candidate never waited in its INSERT")
                    assert asyncio.get_running_loop().time() < deadline, (
                        "repair INSERT never blocked"
                    )
                    await asyncio.sleep(0.01)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert cursors == {"activation": vehicles[0].activation_id}
                assert conn.info.transaction_status is TransactionStatus.IDLE
            finally:
                await blocker.rollback()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            assert await repair() == 2
            rows = await (
                await conn.execute(
                    "SELECT count(*) FROM jobs WHERE dedup_key LIKE 'external-boot:repair:%'"
                )
            ).fetchone()
            assert rows == (3,)

    asyncio.run(body())
