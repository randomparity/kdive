"""Public release admission through the real worker and authority service."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from kdive.domain.operations.jobs import DEFAULT_JOB_DISPATCH_LANE, Job, JobKind
from kdive.jobs.authority_sender import AuthorityRequestSender
from kdive.jobs.external_boot_authority_client import ExternalBootAuthorityClient
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


def test_public_active_release_claims_and_completes_through_worker(
    migrated_url: str,
    authority_role_dsns: Callable[[str], str],
    tmp_path: Path,
) -> None:
    async def run() -> None:
        vehicle = build_vehicle()
        adapter = _ReleaseFaultAuthorityAdapter(vehicle)
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

            async def authenticate(credential: SecretStr) -> AuthenticatedPeer:
                assert credential.get_secret_value() == case.credential
                return AuthenticatedPeer(case.worker_incarnation)

            class Backend:
                async def _request_frame(self, envelope: bytes, *, deadline: float) -> bytes:
                    assert deadline > asyncio.get_running_loop().time()
                    return await _dispatch(envelope, authenticate, service)

            sender = AuthorityRequestSender(Backend, lambda: SecretStr(case.credential))
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

            operations = build_operations(
                ExternalBootHandlerPorts(
                    resolver=provider_resolver(external_boot=None),
                    incarnation_credential=SecretStr(case.credential),
                    secret_registry=SecretRegistry(),
                    authority_client_factory=lambda binding, marker, deadline: (
                        ExternalBootAuthorityClient(sender, marker, deadline)
                    ),
                )
            )
            registry = HandlerRegistry()
            registry.register(JobKind.BOOT, route_marked(operations, must_not_run))
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
                claimed = await worker.run_once(DEFAULT_JOB_DISPATCH_LANE)
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
                    "SELECT state FROM external_boot_authorities WHERE job_id = %s", (claimed.id,)
                )
                assert await cur.fetchone() == ("retired",)
                await cur.execute(
                    "SELECT consumed FROM external_boot_release_cleanup_receipts WHERE job_id = %s",
                    (claimed.id,),
                )
                assert await cur.fetchone() == (True,)
        assert adapter.mutations == ["recover", "cleanup"]
        assert vehicle.port.calls == ["recover", "cleanup"]

    asyncio.run(run())
