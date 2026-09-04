"""Post-prepared external-boot reconciler repair contracts (#2203)."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import psycopg
import pytest
from psycopg.errors import ForeignKeyViolation, InsufficientPrivilege, QueryCanceled
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from kdive.jobs.payloads import BootPayload
from kdive.reconciler.repairs import external_boot
from tests.db.external_boot_authority_support import _RoleDsns
from tests.jobs.handlers.external_boot.conftest import resolver_for
from tests.jobs.handlers.external_boot.seeding import seed_case
from tests.jobs.handlers.external_boot.support import marked_job
from tests.jobs.handlers.external_boot.vehicle import build_vehicle


@pytest.mark.parametrize(
    ("lane", "predicate"),
    [
        ("activation", "activation_readiness_deadline < now()"),
        ("recovery", "recovery_readiness_deadline < now()"),
        ("release", "reservation.state = 'ready'"),
        ("cleanup", "NOT a.cleanup_complete"),
    ],
)
def test_candidate_lanes_use_durable_state(lane: str, predicate: str) -> None:
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
            with pytest.raises(QueryCanceled):
                await external_boot.repair_external_boot_lane(
                    reconciler,
                    lane="activation",
                    resolver=resolver_for(vehicle),
                    authority_instance="authority-current",
                )
            assert reconciler.info.transaction_status is TransactionStatus.IDLE

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
