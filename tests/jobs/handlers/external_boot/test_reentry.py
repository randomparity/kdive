"""Worker external-boot operations resume from authority-owned observations (ADR-0595)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

import psycopg
import pytest
from pydantic import SecretStr

from kdive.domain.operations.jobs import JobKind
from kdive.jobs import queue
from kdive.jobs.handlers.external_boot.ports import ExternalBootHandlerPorts
from kdive.jobs.handlers.external_boot.registrar import build_operations
from kdive.jobs.models import (
    ExternalBootAuthorityFailure,
    ExternalBootAuthorityMarkerV1,
    _RecoveryAttemptResult,
)
from kdive.providers.external_boot_authority.protocol import (
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
)
from tests.jobs.handlers.external_boot.conftest import resolver_for, role_connection
from tests.jobs.handlers.external_boot.seeding import RecordingAcknowledger, seed_case
from tests.jobs.handlers.external_boot.support import CASES, build_job
from tests.jobs.handlers.external_boot.vehicle import build_vehicle


class RecordingExecutor:
    def __init__(
        self, category: Literal["absent", "source", "target", "mixed", "unreadable", "conflict"]
    ) -> None:
        self.category = category
        self.requests: list[AuthorityMutationRequestV1] = []

    async def execute(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1:
        self.requests.append(request)
        return AuthorityObservationV1(
            observation_id=uuid4(), category=self.category, composite_state="sha256:" + "8" * 64
        )


@pytest.mark.parametrize(
    ("operation", "category"),
    [("activate", "target"), ("recover", "source"), ("cleanup", "absent")],
)
def test_worker_uses_authority_observation_without_direct_provider_mutation(
    migrated_url: str,
    authority_role_dsns: Callable[[str], str],
    operation: str,
    category: Literal["absent", "source", "target", "mixed", "unreadable", "conflict"],
) -> None:
    async def main() -> None:
        vehicle = build_vehicle()
        spec = CASES[operation]
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as seed:
            case = await seed_case(
                seed,
                vehicle,
                purpose=spec["purpose"],
                operation=operation,
                activation_state=spec["activation_state"],
                **spec["seed"],
            )
            executor = RecordingExecutor(category)
            ports = ExternalBootHandlerPorts(
                resolver=resolver_for(vehicle),
                incarnation_credential=SecretStr(case.credential),
                acknowledger=RecordingAcknowledger(authority_role_dsns("kdive_provider_authority")),
                authority_executor=executor,
            )
            marker = ExternalBootAuthorityMarkerV1.model_validate(case.marker)
            job = build_job(
                JobKind.TEARDOWN if spec["purpose"] == "teardown" else JobKind.BOOT,
                {
                    "run_id": str(vehicle.run_id),
                    "external_boot_authority_v1": case.marker,
                },
            ).model_copy(update={"id": case.job_id, "attempt": case.attempt})
            handler = build_operations(ports).get(operation)
            assert handler is not None
            async with await role_connection(authority_role_dsns("kdive_worker")) as worker:
                await handler(worker, job, marker)

        assert len(executor.requests) == 1
        assert executor.requests[0].operation.value == operation
        assert vehicle.port.calls == []

    asyncio.run(main())


def test_activate_commits_deadline_before_provider_and_reuses_it(
    migrated_url: str, authority_role_dsns: Callable[[str], str]
) -> None:
    async def main() -> None:
        vehicle = build_vehicle()
        now = datetime(2026, 9, 4, tzinfo=UTC)
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as seed:
            case = await seed_case(seed, vehicle, purpose="activate", activation_state="prepared")
            executor = RecordingExecutor("target")
            ports = ExternalBootHandlerPorts(
                resolver=resolver_for(vehicle),
                incarnation_credential=SecretStr(case.credential),
                acknowledger=RecordingAcknowledger(authority_role_dsns("kdive_provider_authority")),
                authority_executor=executor,
                clock=lambda: now,
                activation_readiness_timeout=timedelta(seconds=90),
            )
            handler = build_operations(ports).get("activate")
            assert handler is not None
            marker = ExternalBootAuthorityMarkerV1.model_validate(case.marker)
            job = build_job(
                JobKind.BOOT,
                {"run_id": str(vehicle.run_id), "external_boot_authority_v1": case.marker},
            ).model_copy(update={"id": case.job_id, "attempt": case.attempt})
            async with await role_connection(authority_role_dsns("kdive_worker")) as worker:
                result = await handler(worker, job, marker)
                assert result.result.operation == "deadline"
                assert executor.requests == []
                committed = await queue.complete_external_boot(
                    worker,
                    job,
                    result,
                    incarnation_credential=SecretStr(case.credential),
                )
                assert committed is not None and committed.state.value == "running"
                replay = await handler(worker, job, marker)
            assert replay.result.operation == "activate"
            assert replay.result.model_dump()["activation_readiness_deadline"] == now + timedelta(
                seconds=90
            )
            assert len(executor.requests) == 1

    asyncio.run(main())


def test_recover_commits_attempt_before_provider_and_reuses_it(
    migrated_url: str, authority_role_dsns: Callable[[str], str]
) -> None:
    async def main() -> None:
        vehicle = build_vehicle()
        now = datetime(2026, 9, 4, tzinfo=UTC)
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as seed:
            case = await seed_case(
                seed,
                vehicle,
                purpose="recover",
                activation_state="active",
                system_state="crashed",
                with_reservation=True,
            )
            executor = RecordingExecutor("source")
            ports = ExternalBootHandlerPorts(
                resolver=resolver_for(vehicle),
                incarnation_credential=SecretStr(case.credential),
                acknowledger=RecordingAcknowledger(authority_role_dsns("kdive_provider_authority")),
                authority_executor=executor,
                clock=lambda: now,
                recovery_readiness_timeout=timedelta(seconds=120),
            )
            handler = build_operations(ports).get("recover")
            assert handler is not None
            marker = ExternalBootAuthorityMarkerV1.model_validate(case.marker)
            job = build_job(
                JobKind.BOOT,
                {"run_id": str(vehicle.run_id), "external_boot_authority_v1": case.marker},
            ).model_copy(update={"id": case.job_id, "attempt": case.attempt})
            async with await role_connection(authority_role_dsns("kdive_worker")) as worker:
                result = await handler(worker, job, marker)
                assert result.result.operation == "recovery-attempt"
                assert executor.requests == []
                committed = await queue.complete_external_boot(
                    worker,
                    job,
                    result,
                    incarnation_credential=SecretStr(case.credential),
                )
                assert committed is not None and committed.state.value == "running"
                replay = await handler(worker, job, marker)
            assert replay.result.operation == "recover"
            assert len(executor.requests) == 1

    asyncio.run(main())


@pytest.mark.parametrize(
    ("operation", "activation_state", "attempt_state", "category"),
    [
        ("activate", "activating", "recovering", "target"),
        ("recover", "recovering", "recovering", "source"),
    ],
)
def test_expired_deadline_returns_a_committable_failure_before_provider(
    migrated_url: str,
    authority_role_dsns: Callable[[str], str],
    operation: str,
    activation_state: str,
    attempt_state: str,
    category: Literal["source", "target"],
) -> None:
    async def main() -> None:
        vehicle = build_vehicle()
        now = datetime(2027, 1, 1, tzinfo=UTC)
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as seed:
            case = await seed_case(
                seed,
                vehicle,
                purpose=operation,
                activation_state=activation_state,
                system_state="crashed" if operation == "recover" else "ready",
                attempt_state=attempt_state,
                with_reservation=operation == "recover",
                with_pre_recovery=operation == "recover",
            )
            column = (
                "activation_readiness_deadline"
                if operation == "activate"
                else "recovery_readiness_deadline"
            )
            table = (
                "external_boot_activations"
                if operation == "activate"
                else "external_boot_recovery_attempts"
            )
            await seed.execute(
                f"UPDATE {table} SET {column} = %s WHERE activation_id = %s"
                if operation == "recover"
                else f"UPDATE {table} SET {column} = %s WHERE id = %s",
                (now, vehicle.activation_id),
            )
            executor = RecordingExecutor(category)
            ports = ExternalBootHandlerPorts(
                resolver=resolver_for(vehicle),
                incarnation_credential=SecretStr(case.credential),
                acknowledger=RecordingAcknowledger(authority_role_dsns("kdive_provider_authority")),
                authority_executor=executor,
                clock=lambda: now,
            )
            handler = build_operations(ports).get(operation)
            assert handler is not None
            marker = ExternalBootAuthorityMarkerV1.model_validate(case.marker)
            job = build_job(
                JobKind.BOOT,
                {"run_id": str(vehicle.run_id), "external_boot_authority_v1": case.marker},
            ).model_copy(update={"id": case.job_id, "attempt": case.attempt})
            async with await role_connection(authority_role_dsns("kdive_worker")) as worker:
                with pytest.raises(ExternalBootAuthorityFailure) as excinfo:
                    await handler(worker, job, marker)
                assert executor.requests == []
                committed = await queue.fail_external_boot(
                    worker,
                    job,
                    excinfo.value.result,
                    incarnation_credential=SecretStr(case.credential),
                )
                assert committed is not None
            row = await seed.execute(
                "SELECT state, recovery_point, pre_recovery_evidence "
                "FROM external_boot_activations WHERE id = %s",
                (vehicle.activation_id,),
            )
            activation_row = await row.fetchone()
            assert activation_row is not None
            state, recovery_point, pre_recovery = activation_row
            assert state == ("recovering" if operation == "activate" else "recovery_failed")
            assert recovery_point is not None
            if operation == "recover":
                assert pre_recovery is not None

    asyncio.run(main())


def test_resolve_conflict_starts_a_fresh_attempt_from_its_own_clock(
    migrated_url: str, authority_role_dsns: Callable[[str], str]
) -> None:
    async def main() -> None:
        vehicle = build_vehicle()
        now = datetime(2026, 9, 4, tzinfo=UTC)
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as seed:
            case = await seed_case(
                seed,
                vehicle,
                purpose="resolve-conflict",
                operation="resolve-conflict",
                activation_state="recovery_conflict",
                attempt_state="conflict",
                with_pre_recovery=True,
            )
            await seed.execute(
                "UPDATE external_boot_recovery_attempts SET recovery_readiness_deadline = %s "
                "WHERE attempt_id = %s",
                (now - timedelta(days=1), case.attempt_id),
            )
            executor = RecordingExecutor("source")
            ports = ExternalBootHandlerPorts(
                resolver=resolver_for(vehicle),
                incarnation_credential=SecretStr(case.credential),
                acknowledger=RecordingAcknowledger(authority_role_dsns("kdive_provider_authority")),
                authority_executor=executor,
                clock=lambda: now,
                recovery_readiness_timeout=timedelta(seconds=120),
            )
            handler = build_operations(ports).get("resolve-conflict")
            assert handler is not None
            marker = ExternalBootAuthorityMarkerV1.model_validate(case.marker)
            job = build_job(
                JobKind.BOOT,
                {"run_id": str(vehicle.run_id), "external_boot_authority_v1": case.marker},
            ).model_copy(update={"id": case.job_id, "attempt": case.attempt})
            async with await role_connection(authority_role_dsns("kdive_worker")) as worker:
                result = await handler(worker, job, marker)
                assert result.result.operation == "recovery-attempt"
                assert isinstance(result.result, _RecoveryAttemptResult)
                assert result.result.deadline == now + timedelta(seconds=120)
                assert result.result.attempt_id != case.attempt_id
                assert executor.requests == []

    asyncio.run(main())
