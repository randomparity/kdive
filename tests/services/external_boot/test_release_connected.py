"""Public release admission through the real worker and authority service."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, LiteralString, cast
from uuid import uuid4

import psycopg
import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from kdive.domain.operations.jobs import DEFAULT_JOB_DISPATCH_LANE, Job, JobKind
from kdive.jobs.authority_sender import AuthorityRequestSender
from kdive.jobs.external_boot_authority_client import ExternalBootAuthorityClient
from kdive.jobs.handlers.external_boot import lifecycle
from kdive.jobs.handlers.external_boot.ports import ExternalBootHandlerPorts
from kdive.jobs.handlers.external_boot.registrar import build_operations
from kdive.jobs.handlers.external_boot.router import route_marked
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.worker import Worker
from kdive.mcp.tools.external_boot.recovery_requests import request_release
from kdive.providers.external_boot_authority.journal import FileAuthorityJournal
from kdive.providers.external_boot_authority.protocol import (
    AuthorityCommitContextV1,
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
)
from kdive.providers.external_boot_authority.repository import DatabaseAuthorityRepository
from kdive.providers.external_boot_authority.service import (
    AuthenticatedPeer,
    ExternalBootAuthorityService,
)
from kdive.providers.external_boot_authority.transport import _dispatch
from kdive.providers.ports.external_boot import OpaqueProviderRef
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.db.external_boot_authority_support import authority_role_dsns as _role_dsns_fixture
from tests.jobs.handlers.external_boot.conftest import resolver_for
from tests.jobs.handlers.external_boot.seeding import seed_case
from tests.jobs.handlers.external_boot.vehicle import Vehicle, build_vehicle
from tests.mcp.lifecycle import runs_support
from tests.mcp.systems_support import provider_resolver


def _ctx() -> RequestContext:
    return RequestContext(
        principal="alice", agent_session="s", projects=("proj",), roles={"proj": Role.ADMIN}
    )


class _ReleaseFaultAuthorityAdapter:
    """Drive derived recovery and cleanup against the actual fault-inject port."""

    def __init__(self, vehicle: Vehicle) -> None:
        self._vehicle = vehicle
        self.mutations: list[str] = []
        self.block_operation: str | None = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    def _observation(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1:
        category = "source" if request.operation.value == "recover" else "absent"
        digest = (
            "sha256:"
            + hashlib.sha256(
                f"{request.operation.value}/{request.authority_id}/{request.generation}".encode()
            ).hexdigest()
        )
        return AuthorityObservationV1(
            observation_id=request.attempt_id, category=category, composite_state=digest
        )

    async def observe(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1:
        return self._observation(request)

    async def commit(
        self, request: AuthorityMutationRequestV1, context: AuthorityCommitContextV1
    ) -> AuthorityObservationV1:
        del context
        authority = OpaqueProviderRef(ref=f"authority/{request.authority_id}/{request.generation}")
        if request.operation.value == "recover":
            self._vehicle.port.recover(self._vehicle.recovery_point, authority)
        else:
            self._vehicle.port.cleanup(self._vehicle.recovery_point, authority)
        self.mutations.append(request.operation.value)
        if request.operation.value == self.block_operation:
            self.entered.set()
            await self.release.wait()
        return self._observation(request)


@pytest.fixture
def authority_role_dsns(migrated_url: str) -> Any:
    fixture = cast(Any, _role_dsns_fixture).__wrapped__(migrated_url)
    yield next(fixture)
    fixture.close()


@asynccontextmanager
async def _authority_connection(dsn: str) -> AsyncIterator[AsyncConnection]:
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as connection:
        yield connection


async def _retire_seed_authority(conn: AsyncConnection, case: Any) -> None:
    """Make the active marker's predecessor the release dispatcher can resolve."""
    await conn.execute("UPDATE jobs SET state = 'succeeded' WHERE id = %s", (case.job_id,))
    await conn.execute(
        "INSERT INTO external_boot_authorities "
        "(system_id, allocation_id, activation_id, run_id, plan_identity, job_id, job_attempt, "
        "purpose, provider_kind, authority_instance, worker_incarnation, operation, "
        "operation_identity, operation_digest, generation, state, acknowledged_at, retired_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,1,'recover','local-libvirt','authority-vehicle',%s,'recover',"
        "%s,%s,1,'retired',now(),now())",
        (
            case.vehicle.system_id,
            case.allocation_id,
            case.vehicle.activation_id,
            case.vehicle.run_id,
            case.vehicle.plan_identity,
            case.job_id,
            case.worker_incarnation,
            "prior-recovery",
            "sha256:" + "2" * 64,
        ),
    )
    await conn.execute(
        "INSERT INTO external_boot_authority_counters (system_id, last_generation) VALUES (%s, 1) "
        "ON CONFLICT (system_id) DO UPDATE SET last_generation = 1",
        (case.vehicle.system_id,),
    )


@pytest.mark.parametrize(
    "interrupt_after", [None, "recover", "cleanup", "cancel-recover", "cancel-cleanup"]
)
def test_public_active_release_claims_and_completes_through_worker(
    migrated_url: str,
    authority_role_dsns: Callable[[str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt_after: Literal["recover", "cleanup", "cancel-recover", "cancel-cleanup"] | None,
) -> None:
    async def run() -> None:
        vehicle = build_vehicle()
        adapter = _ReleaseFaultAuthorityAdapter(vehicle)
        if interrupt_after is not None and interrupt_after.startswith("cancel-"):
            adapter.block_operation = interrupt_after.removeprefix("cancel-")
            adapter.release.clear()
        retry_incarnation = f"docker:release-retry-{uuid4()}"
        retry_credential = f"release-retry-credential-{uuid4()}"
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as seed:
            case = await seed_case(
                seed,
                vehicle,
                purpose="release",
                operation="release",
                activation_state="active",
                with_reservation=True,
            )
            await _retire_seed_authority(seed, case)
            service = ExternalBootAuthorityService(
                repository=DatabaseAuthorityRepository(
                    lambda: _authority_connection(authority_role_dsns("kdive_provider_authority"))
                ),
                journal_factory=lambda system_id: FileAuthorityJournal(
                    tmp_path, f"{system_id}.journal"
                ),
                adapter=adapter,
            )

            credentials = {
                case.credential: case.worker_incarnation,
                retry_credential: retry_incarnation,
            }

            async def authenticate(credential: SecretStr) -> AuthenticatedPeer:
                return AuthenticatedPeer(credentials[credential.get_secret_value()])

            class Backend:
                async def _request_frame(self, envelope: bytes, *, deadline: float) -> bytes:
                    assert deadline > asyncio.get_running_loop().time()
                    return await _dispatch(envelope, authenticate, service)

            def sender_for(credential: str) -> AuthorityRequestSender:
                return AuthorityRequestSender(Backend, lambda: SecretStr(credential))

            async with runs_support.pool(migrated_url) as pool:
                requested = await request_release(
                    pool,
                    _ctx(),
                    run_id=str(vehicle.run_id),
                    resolver=resolver_for(vehicle),
                )
            assert requested.status == "queued", requested.data

            async def must_not_run(_conn: AsyncConnection, _job: Job) -> str:
                raise AssertionError("a marked release reached the ordinary boot handler")

            def registry_for(credential: str) -> HandlerRegistry:
                operations = build_operations(
                    ExternalBootHandlerPorts(
                        resolver=provider_resolver(external_boot=None),
                        incarnation_credential=SecretStr(credential),
                        secret_registry=SecretRegistry(),
                        authority_client_factory=lambda binding, marker, deadline: (
                            ExternalBootAuthorityClient(sender_for(credential), marker, deadline)
                        ),
                    )
                )
                registry = HandlerRegistry()
                registry.register(JobKind.BOOT, route_marked(operations, must_not_run))
                return registry

            registry = registry_for(case.credential)
            interrupted = False
            original_status = lifecycle._derived_release_status

            async def interrupting_status(
                conn: AsyncConnection, sql: LiteralString, args: tuple[object, ...]
            ) -> str:
                nonlocal interrupted
                stage = (
                    "commit_external_boot_derived_release_recovery"
                    if interrupt_after == "recover"
                    else "finalize_external_boot_derived_release"
                )
                if interrupt_after == "cleanup" and not interrupted and stage in sql:
                    interrupted = True
                    raise RuntimeError("injected release interruption")
                status = await original_status(conn, sql, args)
                if interrupt_after in {"recover", "cleanup"} and not interrupted and stage in sql:
                    interrupted = True
                    raise RuntimeError("injected release interruption")
                return status

            monkeypatch.setattr(lifecycle, "_derived_release_status", interrupting_status)
            async with AsyncConnectionPool(
                authority_role_dsns("kdive_worker"), min_size=5, max_size=5
            ) as worker_pool:
                worker = Worker(
                    worker_pool,
                    registry,
                    worker_id=case.worker_incarnation,
                    incarnation_credential=SecretStr(case.credential),
                    secret_registry=SecretRegistry(),
                )
                if adapter.block_operation is None:
                    claimed = await worker.run_once(DEFAULT_JOB_DISPATCH_LANE)
                else:
                    dispatch = asyncio.create_task(worker.run_once(DEFAULT_JOB_DISPATCH_LANE))
                    await adapter.entered.wait()
                    dispatch.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await dispatch
                    adapter.release.set()
                    for _ in range(100):
                        journal = FileAuthorityJournal(tmp_path, f"{vehicle.system_id}.journal")
                        try:
                            records = list(journal.load())
                        finally:
                            journal.close()
                        if any(
                            record.phase.value == "terminal"
                            and record.operation == adapter.block_operation
                            for record in records
                        ):
                            break
                        await asyncio.sleep(0)
                    else:
                        raise AssertionError("cancelled authority phase did not reach terminal")
                    claimed = None
                if interrupt_after is not None:
                    if not interrupt_after.startswith("cancel-"):
                        assert interrupted
                        assert claimed is not None
                    async with seed.cursor() as cur:
                        await cur.execute(
                            "SELECT authority.id, authority.generation, ack.journal_sequence, "
                            "ack.journal_digest, authority.state, authority.job_attempt "
                            "FROM external_boot_authorities AS authority "
                            "JOIN external_boot_authority_acknowledgements AS ack "
                            "ON ack.authority_id = authority.id "
                            "WHERE authority.job_id = %s ORDER BY authority.generation",
                            (requested.object_id,),
                        )
                        old_authority = await cur.fetchone()
                        assert old_authority is not None
                        old_id, old_generation, old_sequence, old_digest, state, old_attempt = (
                            old_authority
                        )
                        expected_state = (
                            "current" if interrupt_after.startswith("cancel-") else "retired"
                        )
                        assert (state, old_attempt) == (expected_state, 1)
                        if not interrupt_after.startswith("cancel-"):
                            await cur.execute(
                                "SELECT operation_identity FROM "
                                "resolve_current_external_boot_release_phase_authority("
                                "%s,%s,%s,%s,%s,%s)",
                                (
                                    case.worker_incarnation,
                                    old_id,
                                    old_generation,
                                    old_sequence,
                                    old_digest,
                                    "cleanup",
                                ),
                            )
                            assert await cur.fetchone() is None
                        if interrupt_after == "cleanup":
                            await cur.execute(
                                "SELECT consumed FROM external_boot_release_cleanup_receipts "
                                "WHERE job_id = %s",
                                (requested.object_id,),
                            )
                            assert await cur.fetchall() == [(False,)]
                    await seed.execute(
                        "INSERT INTO worker_incarnations "
                        "(incarnation, authority_kind, authority_binding, credential_hash, "
                        "fence_protocol) "
                        "VALUES (%s, 'docker', '{}'::jsonb, sha256(convert_to(%s, 'UTF8')), 4)",
                        (retry_incarnation, retry_credential),
                    )
                    retry_worker = Worker(
                        worker_pool,
                        registry_for(retry_credential),
                        worker_id=retry_incarnation,
                        incarnation_credential=SecretStr(retry_credential),
                        secret_registry=SecretRegistry(),
                    )
                    if interrupt_after.startswith("cancel-"):
                        assert await retry_worker.run_once(DEFAULT_JOB_DISPATCH_LANE) is None
                        await seed.execute(
                            "UPDATE jobs SET lease_expires_at = now() - interval '1 min' "
                            "WHERE id = %s",
                            (requested.object_id,),
                        )
                    claimed = await retry_worker.run_once(DEFAULT_JOB_DISPATCH_LANE)
                    assert await worker.run_once(DEFAULT_JOB_DISPATCH_LANE) is None
            assert claimed is not None and str(claimed.id) == requested.object_id

            async with seed.cursor() as cur:
                await cur.execute(
                    "SELECT state, cleanup_complete FROM external_boot_activations WHERE id = %s",
                    (vehicle.activation_id,),
                )
                assert await cur.fetchone() == ("recovered", True)
                await cur.execute("SELECT state FROM jobs WHERE id = %s", (claimed.id,))
                assert await cur.fetchone() == ("succeeded",)
                await cur.execute(
                    "SELECT state FROM external_boot_authorities WHERE job_id = %s "
                    "ORDER BY generation DESC",
                    (claimed.id,),
                )
                assert await cur.fetchone() == ("retired",)
                await cur.execute(
                    "SELECT state, job_attempt, worker_incarnation FROM external_boot_authorities "
                    "WHERE job_id = %s ORDER BY generation",
                    (claimed.id,),
                )
                expected_authorities = (
                    [("retired", 1, case.worker_incarnation)]
                    if interrupt_after is None
                    else [
                        (
                            "superseded" if interrupt_after.startswith("cancel-") else "retired",
                            1,
                            case.worker_incarnation,
                        ),
                        ("retired", 2, retry_incarnation),
                    ]
                )
                assert await cur.fetchall() == expected_authorities
                await cur.execute(
                    "SELECT consumed, adopted_from_root_authority_id IS NOT NULL "
                    "FROM external_boot_release_cleanup_receipts WHERE job_id = %s "
                    "ORDER BY created_at",
                    (claimed.id,),
                )
                expected_receipts = (
                    [(True, False)]
                    if interrupt_after != "cleanup"
                    else [(False, False), (True, True)]
                )
                assert await cur.fetchall() == expected_receipts
                await cur.execute(
                    "SELECT count(*) FROM external_boot_reservation_releases "
                    "WHERE activation_id = %s",
                    (vehicle.activation_id,),
                )
                assert await cur.fetchone() == (1,)
        assert adapter.mutations == ["recover", "cleanup"]
        assert vehicle.port.calls == ["recover", "cleanup"]

    asyncio.run(run())
