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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, LiteralString, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

import kdive.config as config_registry
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
from kdive.providers.fault_inject.lifecycle.external_boot import FaultInjectExternalBoot
from kdive.providers.local_libvirt.external_boot_authority import LocalExternalBootAuthorityAdapter
from kdive.providers.local_libvirt.lifecycle.boot.external_boot import LocalObservedState
from kdive.providers.ports.external_boot import OpaqueProviderRef
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.worker_lifecycle.authority_store import CURRENT_WORKER_FENCE_PROTOCOL
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
        teardown_executor = RecordingTeardownExecutor(seed)
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


_SHA = "sha256:" + "1" * 64


class _PreparingProvider(FaultInjectExternalBoot):
    """Record the worker-owned phases and reopen its prepared recovery point."""

    def __init__(self) -> None:
        super().__init__()
        self.phases: list[str] = []
        self._points: dict[str, Any] = {}
        self._active: set[str] = set()

    def execute_preparation(self, request: Any) -> Any:
        self.phases.append(request.phase)
        return super().execute_preparation(request)

    def prepare(self, materialization: Any, binding: Any, authority: Any) -> Any:
        point = super().prepare(materialization, binding, authority)
        self._points[binding.activation_id] = point
        return point

    def recovery_point(self, binding: Any, authority: Any) -> Any:
        del authority
        return self._points[binding.activation_id]

    def observe_state(self, binding: Any, authority: Any) -> LocalObservedState:
        del authority
        point = self._points[binding.activation_id]
        state = point.target_state if binding.activation_id in self._active else point.source_state
        return LocalObservedState(
            definition=state.definition,
            modules=state.modules,
            active=binding.activation_id in self._active,
        )

    def activate(self, recovery: Any, authority: Any) -> None:
        self.phases.append("activate")
        super().activate(recovery, authority)
        self._active.add(recovery.binding.activation_id)


async def _seed_public_external_boot(pool: AsyncConnectionPool) -> tuple[str, str]:
    system_id = await runs_support.seed_system(pool)
    investigation_id = await runs_support.seed_investigation(pool)
    run_id = str(uuid4())
    generation = uuid4()
    build_ref = f"{'b' * 64}.{generation}"
    evidence = {
        "schema": "external-boot-evidence-v1",
        "architecture": "x86_64",
        "bundle_sha256": _SHA,
        "initrd": {"sha256": _SHA, "size_bytes": 1024},
        "archive_member_count": 3,
        "archive_uncompressed_bytes": 4096,
        "vmlinuz_sha256": _SHA,
        "vmlinuz_size_bytes": 2048,
        "decoded_kernel_size_bytes": 4096,
        "elf_metadata_bytes": 512,
        "gnu_build_id_size_bytes": 8,
        "release": "6.9.0-kdive",
        "module_source_manifest": _SHA,
        "module_member_count": 2,
        "module_uncompressed_bytes": 64,
    }
    root_spec = {
        "schema": "root-spec-v1",
        "architecture": "x86_64",
        "root": "UUID=authority-root",
        "arguments": ["root=UUID=authority-root", "rootfstype=xfs"],
        "authority": "stage-inspection",
        "source": {"kind": "staged-image", "identity": _SHA},
    }
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO investigation_builds "
            "(investigation_id, generation, build_ref, content_digest, canonical_document, "
            "build_result, artifacts, target_kind, build_profile, state, expires_at) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,'local-libvirt',%s,'active',%s)",
            (
                investigation_id,
                generation,
                build_ref,
                "b" * 64,
                Jsonb({"version": 2, "external_boot_evidence": evidence}),
                Jsonb(
                    {
                        "kernel_ref": "builds/kernel.tar",
                        "initrd_ref": "builds/initrd.img",
                    }
                ),
                Jsonb(
                    {
                        "kernel": {"version_id": "kernel-v1"},
                        "initrd": {"version_id": "initrd-v1"},
                    }
                ),
                Jsonb({"schema_version": 1, "arch": "x86_64"}),
                datetime.now(UTC) + timedelta(days=1),
            ),
        )
        await conn.execute(
            "INSERT INTO runs "
            "(id, investigation_id, system_id, target_kind, state, build_profile, build_ref, "
            "principal, project) VALUES "
            "(%s,%s,%s,'local-libvirt','succeeded',%s,%s,'user-1','proj')",
            (run_id, investigation_id, system_id, Jsonb({"schema_version": 1}), build_ref),
        )
        await conn.execute(
            "INSERT INTO system_root_provenance "
            "(system_id, source_image_id, project, architecture, image_digest, root_spec) "
            "VALUES (%s,%s,'proj','x86_64',%s,%s)",
            (system_id, uuid4(), _SHA, Jsonb(root_spec)),
        )
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s,'install','succeeded','{}'::jsonb)",
            (run_id,),
        )
    return run_id, system_id


def _configure_external_boot() -> None:
    config_registry.load(
        {
            "KDIVE_EXTERNAL_BOOT_AUTHORITY_INSTANCE": AUTHORITY_INSTANCE,
            "KDIVE_EXTERNAL_BOOT_AUTHORITY_STORE_IDENTITY": "store/public-boot",
            "KDIVE_EXTERNAL_BOOT_AUTHORITY_RECOVERY_RESERVE_BYTES": "4096",
            "KDIVE_EXTERNAL_BOOT_AUTHORITY_RECOVERY_MAX_BYTES": "8192",
            "KDIVE_LIBVIRT_EXTERNAL_BOOT_CAPACITY_BYTES": "4096",
        }
    )


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
            authority = await _one(
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
        job = await _one(conn, "SELECT state, attempt FROM jobs WHERE id=%s", (job_id,))
        activation = await _one(
            conn,
            "SELECT id, state FROM external_boot_activations WHERE system_id=%s AND run_id=%s",
            (system_id, run_id),
        )
        authority_rows = await _one(
            conn,
            "SELECT count(*) AS count, max(generation) AS generation "
            "FROM external_boot_authorities WHERE activation_id=%s",
            (activation["id"],),
        )
    return job, activation, authority_rows


@pytest.mark.parametrize("interrupted_phase", [None, "materialize", "prepare"])
def test_public_preparing_boot_stays_on_one_worker_claim_through_preparation(
    migrated_url: str,
    authority_role_dsns: Callable[[str], str],
    tmp_path: Path,
    interrupted_phase: str | None,
) -> None:
    async def body() -> None:
        _configure_external_boot()
        provider = _PreparingProvider()
        if interrupted_phase is not None:
            provider.interrupt_after_receipt(interrupted_phase)
        resolver = provider_resolver(external_boot=provider)
        async with AsyncConnectionPool(migrated_url, min_size=2, max_size=6) as pool:
            run_id, system_id = await _seed_public_external_boot(pool)
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
            await _register_incarnation(pool, worker_id)
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
                lane = (await _one(conn, "SELECT dispatch_lane FROM jobs WHERE id=%s", (job_id,)))[
                    "dispatch_lane"
                ]
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
