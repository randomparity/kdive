"""Connected conflict admission and execution through the durable authority boundary."""

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
from pydantic import SecretStr

from kdive.domain.operations.jobs import Job
from kdive.jobs import queue
from kdive.jobs.authority_sender import AuthorityRequestSender
from kdive.jobs.external_boot_authority_client import ExternalBootAuthorityClient
from kdive.jobs.handlers.external_boot.ports import ExternalBootHandlerPorts
from kdive.jobs.handlers.external_boot.registrar import build_operations
from kdive.jobs.models import ExternalBootAuthorityMarkerV1
from kdive.mcp.tools.external_boot.recovery_requests import resolve_conflict
from kdive.providers.external_boot_authority.journal import FileAuthorityJournal
from kdive.providers.external_boot_authority.protocol import (
    AuthorityCommitContextV1,
    AuthorityConflictResolutionRequestV1,
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
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.db.external_boot_authority_support import authority_role_dsns as _role_dsns_fixture
from tests.jobs.handlers.external_boot.conftest import resolver_for, role_connection
from tests.jobs.handlers.external_boot.seeding import seed_case
from tests.jobs.handlers.external_boot.vehicle import Vehicle, build_vehicle
from tests.mcp.lifecycle import runs_support
from tests.mcp.systems_support import provider_resolver
from tests.services.external_boot.test_recovery_requests import _ctx


class _FaultAuthorityAdapter:
    """Authority adapter that drives the real fault-inject external-boot port."""

    def __init__(self, vehicle: Vehicle) -> None:
        self.vehicle = vehicle
        self.mutations = 0

    def _observation(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1:
        observed = self.vehicle.port.observe(
            self.vehicle.recovery_point,
            OpaqueProviderRef(ref=f"authority/{request.authority_id}"),
        )
        digest = "sha256:" + hashlib.sha256(observed.model_dump_json().encode()).hexdigest()
        return AuthorityObservationV1(
            observation_id=request.attempt_id,
            category="source",
            composite_state=digest,
        )

    async def observe(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1:
        return self._observation(request)

    async def commit(
        self, request: AuthorityMutationRequestV1, context: AuthorityCommitContextV1
    ) -> AuthorityObservationV1:
        del context
        self.mutations += 1
        self.vehicle.port.recover(
            self.vehicle.recovery_point,
            OpaqueProviderRef(ref=f"authority/{request.authority_id}"),
        )
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
    """Turn the seeder's running job into the retired generation that caused the conflict."""
    await conn.execute("UPDATE jobs SET state='succeeded' WHERE id=%s", (case.job_id,))
    await conn.execute(
        "INSERT INTO external_boot_authorities "
        "(system_id, allocation_id, activation_id, run_id, plan_identity, job_id, job_attempt, "
        "purpose, provider_kind, authority_instance, worker_incarnation, operation, "
        "operation_identity, operation_digest, generation, state, acknowledged_at, retired_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,1,'recover','local-libvirt','provider-1',%s,'recover',"
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
        "ON CONFLICT (system_id) DO UPDATE SET last_generation=1",
        (case.vehicle.system_id,),
    )


def test_mcp_conflict_job_claims_and_converges_through_authority_service(
    migrated_url: str,
    authority_role_dsns: Callable[[str], str],
    tmp_path: Path,
) -> None:
    async def run() -> None:
        vehicle = build_vehicle()
        adapter = _FaultAuthorityAdapter(vehicle)
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as seed:
            case = await seed_case(
                seed,
                vehicle,
                purpose="resolve-conflict",
                operation="resolve-conflict",
                activation_state="recovery_conflict",
                attempt_state="conflict",
                system_state="crashed",
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

            resolver = resolver_for(vehicle)
            async with runs_support.pool(migrated_url) as pool:
                observed = adapter._observation(
                    AuthorityConflictResolutionRequestV1.model_validate(
                        case.marker
                        | {
                            "authority_id": "00000000-0000-0000-0000-000000000001",
                            "generation": 1,
                            "operation_digest": "sha256:" + "3" * 64,
                            "attempt_id": "00000000-0000-0000-0000-000000000002",
                            "expected_source_identity": (
                                vehicle.recovery_point.source_state.definition
                            ),
                            "intended_target_identity": (
                                vehicle.recovery_point.target_state.definition
                            ),
                            "recovery_objects": [],
                            "expected_observed_composite": "sha256:" + "0" * 64,
                        }
                    )
                ).composite_state
                first = await resolve_conflict(
                    pool,
                    _ctx(),
                    resolver=resolver,
                    system_id=str(vehicle.system_id),
                    operation="restore-recorded-source",
                    observed_identity=observed,
                )
                replay = await resolve_conflict(
                    pool,
                    _ctx(),
                    resolver=resolver,
                    system_id=str(vehicle.system_id),
                    operation="restore-recorded-source",
                    observed_identity=observed,
                )
            assert first.object_id == replay.object_id

            async with await role_connection(authority_role_dsns("kdive_worker")) as worker:
                claimed = await queue.dequeue(
                    worker,
                    case.worker_incarnation,
                    incarnation_credential=SecretStr(case.credential),
                )
                assert claimed is not None and str(claimed.id) == first.object_id
                marker = ExternalBootAuthorityMarkerV1.model_validate(
                    claimed.payload["external_boot_authority_v1"]
                )
                handler = build_operations(
                    ExternalBootHandlerPorts(
                        resolver=provider_resolver(external_boot=None),
                        incarnation_credential=SecretStr(case.credential),
                        secret_registry=SecretRegistry(),
                        authority_client_factory=lambda binding, marker, deadline: (
                            ExternalBootAuthorityClient(sender, marker, deadline)
                        ),
                    )
                ).get("resolve-conflict")
                assert handler is not None
                result = await handler(worker, claimed, marker)
                if result.result.operation == "recovery-attempt":
                    interim = await queue.complete_external_boot(
                        worker,
                        claimed,
                        result,
                        incarnation_credential=SecretStr(case.credential),
                    )
                    assert interim is not None
                    result = await handler(worker, claimed, marker)
                completed = await queue.complete_external_boot(
                    worker,
                    claimed,
                    result,
                    incarnation_credential=SecretStr(case.credential),
                )
                assert isinstance(completed, Job) and completed.state.value == "succeeded"
            row = await seed.execute(
                "SELECT state FROM external_boot_activations WHERE id=%s", (vehicle.activation_id,)
            )
            assert await row.fetchone() == ("recovered",)
            assert adapter.mutations == 1

    asyncio.run(run())
