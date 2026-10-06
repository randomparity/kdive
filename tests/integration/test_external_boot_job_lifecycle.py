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
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import queue
from kdive.jobs.handlers.external_boot.admission import build_external_boot_payload
from kdive.jobs.handlers.external_boot.ports import ExternalBootHandlerPorts
from kdive.jobs.handlers.external_boot.registrar import build_operations
from kdive.jobs.handlers.external_boot.router import route_marked
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import Authorizing
from kdive.jobs.worker import Worker
from kdive.mcp.tools.lifecycle.runs.steps import boot_run
from kdive.providers.external_boot_authority.journal import FileAuthorityJournal
from kdive.providers.external_boot_authority.protocol import (
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
    AuthorityPreparationMutationRequestV1,
    AuthorityPreparationResponseV1,
)
from kdive.providers.external_boot_authority.repository import DatabaseAuthorityRepository
from kdive.providers.external_boot_authority.service import (
    AuthenticatedPeer,
    ExternalBootAuthorityService,
)
from kdive.providers.local_libvirt.external_boot_authority import LocalExternalBootAuthorityAdapter
from kdive.providers.ports.external_boot import OpaqueProviderRef
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.integration.external_boot_support import (
    CREDENTIAL,
    PreparingProvider,
    configure_external_boot,
    fetch_one,
    register_incarnation,
    seed_public_external_boot,
)
from tests.jobs.handlers.external_boot.conftest import resolver_for
from tests.jobs.handlers.external_boot.seeding import (
    AUTHORITY_INSTANCE,
    RecordingAcknowledger,
    seed_case,
)
from tests.jobs.handlers.external_boot.support import RecordingTeardownExecutor
from tests.jobs.handlers.external_boot.vehicle import Vehicle, build_vehicle
from tests.mcp.lifecycle import runs_support
from tests.mcp.systems_support import provider_resolver
from tests.support.object_store import INERT_OBJECT_STORE

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
        "seed": {"attempt_state": "failed", "with_reservation": True},
        "after": "torn_down",
    },
}


class _VehicleExecutor:
    def __init__(self, vehicle: Vehicle) -> None:
        self.vehicle = vehicle

    async def execute(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1:
        authority = OpaqueProviderRef(
            ref=f"authority/{request.authority_id}/{request.generation}/{request.attempt_id}"
        )
        if request.operation.value == "activate":
            self.vehicle.port.activate(self.vehicle.recovery_point, authority)
            self.vehicle.port.observe(self.vehicle.recovery_point, authority)
            category = "target"
        else:
            self.vehicle.port.cleanup(self.vehicle.recovery_point, authority)
            category = "absent"
        return AuthorityObservationV1(
            observation_id=uuid4(), category=category, composite_state="sha256:" + "8" * 64
        )


def _registry(
    vehicle: Vehicle,
    dsns: Callable[[str], str],
    teardown_executor: RecordingTeardownExecutor,
) -> HandlerRegistry:
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
            authority_executor=_VehicleExecutor(vehicle),
            teardown_executor=teardown_executor,
            artifact_store=INERT_OBJECT_STORE,
        )
    )
    registry = HandlerRegistry()
    registry.register(JobKind.BOOT, route_marked(operations, must_not_run))
    registry.register(JobKind.TEARDOWN, route_marked(operations, must_not_run))
    return registry


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
        teardown_executor = RecordingTeardownExecutor(seed)
        async with AsyncConnectionPool(migrated_url, min_size=2, max_size=6) as pool:
            await register_incarnation(pool, worker_id)
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn,
                    kind,
                    payload,
                    Authorizing(principal="p", agent_session=None, project="proj"),
                    f"external-boot-e2e-{arm}-{vehicle.activation_id}",
                )
                lane = (
                    await fetch_one(conn, "SELECT dispatch_lane FROM jobs WHERE id = %s", (job.id,))
                )["dispatch_lane"]
                # What #2201's 0127 migration bought: a marked payload is claimable again.
                assert await queue.count_claimable(conn, accepted_lanes=[lane]) >= 1

            worker = Worker(
                pool,
                _registry(vehicle, authority_role_dsns, teardown_executor),
                worker_id=worker_id,
                incarnation_credential=CREDENTIAL,
                secret_registry=SecretRegistry(),
            )
            claimed = await worker.run_once(lane)

        assert claimed is not None
        assert claimed.id == job.id
        if arm == "teardown":
            assert len(teardown_executor.calls) == 1
            assert teardown_executor.calls[0].system_id == vehicle.system_id
            assert vehicle.port.calls == []
        else:
            assert vehicle.port.calls, "the worker dispatched nothing to the operation handler"

        activation = await fetch_one(
            seed,
            "SELECT state, cleanup_complete FROM external_boot_activations WHERE id = %s",
            (vehicle.activation_id,),
        )
        assert activation["state"] == spec["after"]
        assert (await fetch_one(seed, "SELECT state FROM jobs WHERE id = %s", (job.id,)))[
            "state"
        ] == "succeeded"

    _drive(migrated_url, body)


class _PublicBootAuthority:
    def __init__(
        self, service: ExternalBootAuthorityService, peer: AuthenticatedPeer, dsn: str
    ) -> None:
        self._service = service
        self._peer = peer
        self._dsn = dsn

    @asynccontextmanager
    async def _connection(self) -> Any:
        async with await psycopg.AsyncConnection.connect(self._dsn, autocommit=True) as connection:
            yield connection

    async def acknowledge(self, request: Any) -> Any:
        answer = await self._service.acknowledge_takeover(self._peer, request)
        async with self._connection() as connection:
            authority = await fetch_one(
                connection,
                "SELECT allocation_id, job_id, job_attempt, worker_incarnation "
                "FROM external_boot_authorities WHERE id=%s",
                (request.authority_id,),
            )
            committed = await connection.execute(
                "SELECT status FROM acknowledge_external_boot_authority("
                "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    request.authority_id,
                    request.generation,
                    authority["allocation_id"],
                    request.activation_id,
                    request.run_id,
                    request.system_id,
                    request.plan_identity,
                    authority["job_id"],
                    authority["job_attempt"],
                    request.purpose,
                    request.provider_kind,
                    request.authority_instance,
                    authority["worker_incarnation"],
                    request.operation.value,
                    request.operation_identity,
                    request.operation_digest,
                    answer.journal_sequence,
                    answer.journal_digest,
                    answer.positive_quiescence_digest,
                ),
            )
            assert await committed.fetchone() == ("applied",)
        return answer

    async def execute_preparation(
        self, request: AuthorityPreparationMutationRequestV1
    ) -> AuthorityPreparationResponseV1:
        return await self._service.execute_preparation(self._peer, request)

    async def execute(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1:
        return await self._service.execute_mutation(self._peer, request)


def _public_boot_registry(resolver: Any, authority: _PublicBootAuthority) -> HandlerRegistry:
    async def must_not_run(_conn: AsyncConnection, _job: Job) -> str:
        raise AssertionError("authority-marked boot reached ordinary handler")

    operations = build_operations(
        ExternalBootHandlerPorts(
            resolver=resolver,
            incarnation_credential=CREDENTIAL,
            secret_registry=SecretRegistry(),
            acknowledger=authority,
            authority_executor=authority,
            preparation_executor=authority,
        )
    )
    registry = HandlerRegistry()
    registry.register(JobKind.BOOT, route_marked(operations, must_not_run))
    return registry


async def _public_boot_rows(
    pool: AsyncConnectionPool, job_id: UUID, system_id: str, run_id: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    async with pool.connection() as conn:
        job = await fetch_one(conn, "SELECT state, attempt FROM jobs WHERE id=%s", (job_id,))
        activation = await fetch_one(
            conn,
            "SELECT id, state FROM external_boot_activations WHERE system_id=%s AND run_id=%s",
            (system_id, run_id),
        )
        authority_rows = await fetch_one(
            conn,
            "SELECT count(*) AS count, max(generation) AS generation "
            "FROM external_boot_authorities WHERE activation_id=%s",
            (activation["id"],),
        )
    return job, activation, authority_rows


@pytest.mark.parametrize("initrd_state", ["null", "missing"])
def test_boot_without_initrd_and_provider_root_refuses_before_activation(
    migrated_url: str,
    initrd_state: Literal["null", "missing"],
) -> None:
    async def body() -> None:
        configure_external_boot()
        resolver = provider_resolver(external_boot=PreparingProvider(), platform_root_cmdline=None)
        async with AsyncConnectionPool(migrated_url, min_size=2, max_size=6) as pool:
            run_id, _ = await seed_public_external_boot(pool, initrd_state=initrd_state)
            response = await boot_run(pool, runs_support.ctx(), run_id, resolver=resolver)
            async with pool.connection() as conn:
                row = await fetch_one(
                    conn,
                    "SELECT count(*) AS n FROM external_boot_activations WHERE run_id=%s",
                    (run_id,),
                )

        assert response.status == "error"
        assert response.error_category == "configuration_error"
        assert response.data["reason"] == "remote_external_boot_initrd_required"
        assert "create a new Run, supply an initrd" in (response.detail or "")
        assert row["n"] == 0

    asyncio.run(body())


@pytest.mark.parametrize("interrupted_phase", [None, "materialize", "prepare"])
def test_public_preparing_boot_stays_on_one_worker_claim_through_preparation(
    migrated_url: str,
    authority_role_dsns: Callable[[str], str],
    tmp_path: Path,
    interrupted_phase: str | None,
) -> None:
    async def body() -> None:
        configure_external_boot()
        provider = PreparingProvider()
        if interrupted_phase is not None:
            provider.interrupt_after_receipt(interrupted_phase)
        resolver = provider_resolver(external_boot=provider)
        async with AsyncConnectionPool(migrated_url, min_size=2, max_size=6) as pool:
            run_id, system_id = await seed_public_external_boot(pool)
            admitted = await boot_run(pool, runs_support.ctx(), run_id, resolver=resolver)
            assert admitted.status == "queued"
            job_id = UUID(admitted.object_id)
            authority_dsn = authority_role_dsns("kdive_provider_authority")

            @asynccontextmanager
            async def authority_connection() -> Any:
                async with await psycopg.AsyncConnection.connect(
                    authority_dsn, autocommit=True
                ) as connection:
                    yield connection

            adapter = LocalExternalBootAuthorityAdapter(cast(Any, provider))
            service = ExternalBootAuthorityService(
                repository=DatabaseAuthorityRepository(authority_connection),
                journal_factory=lambda owned_system: FileAuthorityJournal(
                    tmp_path, f"{owned_system}.journal"
                ),
                adapter=adapter,
            )
            worker_id = "local:public-preparing-boot"
            await register_incarnation(pool, worker_id)
            peer = AuthenticatedPeer(worker_id)
            authority = _PublicBootAuthority(service, peer, authority_dsn)
            worker = Worker(
                pool,
                _public_boot_registry(resolver, authority),
                worker_id=worker_id,
                incarnation_credential=CREDENTIAL,
                secret_registry=SecretRegistry(),
            )
            async with pool.connection() as conn:
                lane = (
                    await fetch_one(conn, "SELECT dispatch_lane FROM jobs WHERE id=%s", (job_id,))
                )["dispatch_lane"]
            try:
                claimed = await worker.run_once(lane)
            finally:
                await service.close()
            job, activation, authority_rows = await _public_boot_rows(
                pool, job_id, system_id, run_id
            )

        assert claimed is not None and claimed.id == job_id
        assert job["attempt"] == 1
        assert authority_rows == {"count": 1, "generation": 1}
        if interrupted_phase is not None:
            assert job["state"] != "succeeded"
            assert activation["state"] == "preparing"
            expected = ["materialize"]
            if interrupted_phase == "prepare":
                expected.append("prepare")
            assert provider.phases == expected
        else:
            assert job["state"] == "succeeded"
            assert activation["state"] == "active"
            assert provider.phases == ["materialize", "prepare", "activate"]

    asyncio.run(body())
