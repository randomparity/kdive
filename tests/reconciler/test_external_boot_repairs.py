"""Post-prepared external-boot reconciler repair contracts (#2203)."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege

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
    marker = source.payload["external_boot_authority_v1"]
    candidate = external_boot._Candidate(
        activation_id,
        marker["system_id"],
        marker["run_id"],
        marker["plan_identity"],
        source.authorizing["project"],
        "activate",
    )
    payload = BootPayload.model_validate(source.payload)
    build = AsyncMock(return_value=(source.kind, payload))
    enqueue = AsyncMock(return_value=(source, True))
    monkeypatch.setattr(external_boot, "_live_successor_exists", AsyncMock(return_value=False))
    monkeypatch.setattr(external_boot, "_source_job", AsyncMock(return_value=source))
    monkeypatch.setattr(external_boot, "build_external_boot_payload", build)
    monkeypatch.setattr(external_boot.queue, "enqueue_with_status", enqueue)

    repaired = asyncio.run(
        external_boot._enqueue_candidate(
            cast(Any, object()),
            candidate,
            resolver=cast(Any, object()),
            authority_instance="authority-current",
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
    source = AsyncMock()
    monkeypatch.setattr(external_boot, "_live_successor_exists", AsyncMock(return_value=True))
    monkeypatch.setattr(external_boot, "_source_job", source)

    repaired = asyncio.run(
        external_boot._enqueue_candidate(
            cast(Any, object()),
            candidate,
            resolver=cast(Any, object()),
            authority_instance="authority-current",
        )
    )

    assert repaired is False
    source.assert_not_awaited()


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
        async with await psycopg.AsyncConnection.connect(
            authority_role_dsns("kdive_reconciler"), autocommit=True
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
