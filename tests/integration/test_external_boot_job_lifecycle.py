"""Charter criterion 9: an authority-marked job, end to end, through a real ``Worker`` claim.

Not a direct handler call. The job is enqueued through ``queue.enqueue``, counted by
``count_claimable_worker_jobs``, claimed by a real worker, dispatched through ``route_marked`` to
the real operations registry, and committed by the worker's own ``_finalize_handler``. Both kinds
are exercised — ``boot`` for ``activate`` and ``teardown`` for ``teardown``.

**What this does not cover, stated because a bite proof showed it.** The registry here is built by
this module, not by ``register_all_handlers``, so un-wrapping the ``JobKind.BOOT`` binding in
``kdive.jobs.handlers.runs.registrar`` leaves these tests green. That wiring is covered by
``tests/jobs/handlers/external_boot/test_operations.py::
test_production_registry_resolves_every_operation_to_one_handler``, which drives
``build_production_handler_registry`` and does turn red for that fault, in both registrars. The
division is deliberate — this test owns the claim-to-commit path, that one owns the registration
path — but it is written down rather than left for a reader to assume this test covers both.

The claimability assertion is not decoration: ``0122_external_boot_authority.sql:293-303``
excluded every marked payload from ``claim_worker_job`` and ``count_claimable_worker_jobs`` until
#2201's ``0127`` migration reopened that half. This asserts what that migration bought rather than
assuming it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, LiteralString

import psycopg
import pytest
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import queue
from kdive.jobs.handlers.external_boot.admission import build_external_boot_payload
from kdive.jobs.handlers.external_boot.ports import ExternalBootHandlerPorts
from kdive.jobs.handlers.external_boot.registrar import build_operations
from kdive.jobs.handlers.external_boot.router import route_marked
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import Authorizing
from kdive.jobs.worker import Worker
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.worker_lifecycle.authority_store import CURRENT_WORKER_FENCE_PROTOCOL
from tests.jobs.handlers.external_boot.conftest import resolver_for
from tests.jobs.handlers.external_boot.seeding import (
    AUTHORITY_INSTANCE,
    RecordingAcknowledger,
    seed_case,
)
from tests.jobs.handlers.external_boot.vehicle import Vehicle, build_vehicle

CREDENTIAL = SecretStr("external-boot-e2e-incarnation-credential")

ARMS: dict[str, dict[str, Any]] = {
    "activate": {
        "purpose": "activate",
        "kind": JobKind.BOOT,
        "activation_state": "activating",
        "seed": {},
        "after": "active",
    },
    "teardown": {
        "purpose": "teardown",
        "kind": JobKind.TEARDOWN,
        "activation_state": "recovery_failed",
        "seed": {"attempt_state": "failed", "with_release": True},
        "after": "recovery_failed",
    },
}


def _registry(vehicle: Vehicle, dsns: Callable[[str], str]) -> HandlerRegistry:
    """The production routing shape: one operations registry behind ``route_marked``.

    The two ordinary handlers are stand-ins that fail loudly rather than the real ones, because a
    marked job reaching either is the failure this wiring exists to prevent — running them for real
    would boot a Run or tear a System down before the assertion could speak.
    """

    async def must_not_run(_conn: AsyncConnection, job: Job) -> str:
        raise AssertionError(f"a marked {job.kind.value} job reached the ordinary handler")

    operations = build_operations(
        ExternalBootHandlerPorts(
            resolver=resolver_for(vehicle),
            incarnation_credential=CREDENTIAL,
            secret_registry=SecretRegistry(),
            acknowledger=RecordingAcknowledger(dsns("kdive_provider_authority")),
        )
    )
    registry = HandlerRegistry()
    registry.register(JobKind.BOOT, route_marked(operations, must_not_run))
    registry.register(JobKind.TEARDOWN, route_marked(operations, must_not_run))
    return registry


async def _register_incarnation(pool: AsyncConnectionPool, worker_id: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO worker_incarnations (incarnation, authority_kind, authority_binding, "
            "fence_protocol, credential_hash) VALUES "
            "(%s, 'local', '{}'::jsonb, %s, sha256(convert_to(%s, 'UTF8'))) "
            "ON CONFLICT (incarnation) DO NOTHING",
            (worker_id, CURRENT_WORKER_FENCE_PROTOCOL, CREDENTIAL.get_secret_value()),
        )


async def _one(conn: AsyncConnection, sql: LiteralString, args: tuple[Any, ...]) -> dict[str, Any]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, args)
        row = await cur.fetchone()
    assert row is not None
    return dict(row)


def _drive(migrated_url: str, body: Callable[[AsyncConnection], Awaitable[None]]) -> None:
    async def _main() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            await body(conn)

    asyncio.run(_main())


@pytest.mark.parametrize("arm", list(ARMS))
def test_a_marked_job_is_claimed_run_and_committed_by_a_real_worker(
    migrated_url: str, authority_role_dsns: Callable[[str], str], arm: str
) -> None:
    spec = ARMS[arm]

    async def body(seed: AsyncConnection) -> None:
        vehicle = build_vehicle()
        case = await seed_case(
            seed,
            vehicle,
            purpose=spec["purpose"],
            operation=arm,
            activation_state=spec["activation_state"],
            **spec["seed"],
        )
        # The seeded job row exists only to satisfy seed_case; this arm enqueues its own through
        # the production path, so the seeded one is removed rather than left to be claimed first.
        await seed.execute("DELETE FROM jobs WHERE id = %s", (case.job_id,))

        kind, payload = await build_external_boot_payload(
            seed,
            activation_id=vehicle.activation_id,
            purpose=spec["purpose"],
            operation=arm,
            provider_kind="local-libvirt",
            authority_instance=AUTHORITY_INSTANCE,
            operation_identity=f"{arm}-e2e",
            resolver=resolver_for(vehicle),
        )
        assert kind is spec["kind"]

        worker_id = f"local:external-boot-e2e-{arm}"
        async with AsyncConnectionPool(migrated_url, min_size=2, max_size=6) as pool:
            await _register_incarnation(pool, worker_id)
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn,
                    kind,
                    payload,
                    Authorizing(principal="p", agent_session=None, project="proj"),
                    f"external-boot-e2e-{arm}-{vehicle.activation_id}",
                )
                lane = (
                    await _one(conn, "SELECT dispatch_lane FROM jobs WHERE id = %s", (job.id,))
                )["dispatch_lane"]
                # What #2201's 0127 migration bought: a marked payload is claimable again.
                assert await queue.count_claimable(conn, accepted_lanes=[lane]) >= 1

            worker = Worker(
                pool,
                _registry(vehicle, authority_role_dsns),
                worker_id=worker_id,
                incarnation_credential=CREDENTIAL,
                secret_registry=SecretRegistry(),
            )
            claimed = await worker.run_once(lane)

        assert claimed is not None
        assert claimed.id == job.id
        assert vehicle.port.calls, "the worker dispatched nothing to the operation handler"

        activation = await _one(
            seed,
            "SELECT state, cleanup_complete FROM external_boot_activations WHERE id = %s",
            (vehicle.activation_id,),
        )
        assert activation["state"] == spec["after"]
        assert (await _one(seed, "SELECT state FROM jobs WHERE id = %s", (job.id,)))[
            "state"
        ] == "succeeded"

    _drive(migrated_url, body)
