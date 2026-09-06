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
from kdive.providers.ports.external_boot import OpaqueProviderRef
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.db.external_boot_authority_support import authority_role_dsns as _role_dsns_fixture
from tests.jobs.handlers.external_boot.conftest import resolver_for, role_connection
from tests.jobs.handlers.external_boot.seeding import seed_case
from tests.jobs.handlers.external_boot.vehicle import Vehicle, build_vehicle
from tests.mcp.lifecycle import runs_support
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

            class Authority:
                async def acknowledge(self, request: Any) -> Any:
                    answer = await service.acknowledge_takeover(
                        AuthenticatedPeer(case.worker_incarnation), request
                    )
                    row = await seed.execute(
                        "SELECT allocation_id, job_id, job_attempt, worker_incarnation "
                        "FROM external_boot_authorities WHERE id=%s",
                        (request.authority_id,),
                    )
                    binding = await row.fetchone()
                    assert binding is not None
                    async with _authority_connection(
                        authority_role_dsns("kdive_provider_authority")
                    ) as connection:
                        committed = await connection.execute(
                            "SELECT status FROM acknowledge_external_boot_authority("
                            "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            (
                                request.authority_id,
                                request.generation,
                                binding[0],
                                request.activation_id,
                                request.run_id,
                                request.system_id,
                                request.plan_identity,
                                binding[1],
                                binding[2],
                                request.purpose,
                                request.provider_kind,
                                request.authority_instance,
                                binding[3],
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

                async def execute_conflict_resolution(
                    self, request: AuthorityConflictResolutionRequestV1
                ) -> AuthorityObservationV1:
                    return await service.execute_conflict_resolution(
                        AuthenticatedPeer(case.worker_incarnation), request
                    )

                async def execute(
                    self, request: AuthorityMutationRequestV1
                ) -> AuthorityObservationV1:
                    return await service.execute_mutation(
                        AuthenticatedPeer(case.worker_incarnation), request
                    )

                async def observe(
                    self, request: AuthorityMutationRequestV1
                ) -> AuthorityObservationV1:
                    return await service.observe_authority(
                        AuthenticatedPeer(case.worker_incarnation), request
                    )

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
                authority = Authority()
                handler = build_operations(
                    ExternalBootHandlerPorts(
                        resolver=resolver,
                        incarnation_credential=SecretStr(case.credential),
                        secret_registry=SecretRegistry(),
                        acknowledger=authority,
                        authority_executor=authority,
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
