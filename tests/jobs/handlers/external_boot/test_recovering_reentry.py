"""A re-entered recover or resolve-conflict gets its own readiness budget (#3410, ADR-0684)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from pydantic import SecretStr

from kdive.domain.operations.jobs import JobKind
from kdive.jobs.handlers.external_boot.ports import ExternalBootHandlerPorts
from kdive.jobs.handlers.external_boot.registrar import build_operations
from kdive.jobs.models import ExternalBootAuthorityMarkerV1, ExternalBootAuthoritySuccessV1
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.jobs.handlers.external_boot.conftest import resolver_for, role_connection
from tests.jobs.handlers.external_boot.seeding import RecordingAcknowledger, seed_case
from tests.jobs.handlers.external_boot.support import build_job
from tests.jobs.handlers.external_boot.test_reentry import RecordingExecutor
from tests.jobs.handlers.external_boot.vehicle import build_vehicle

# The seeded recovering attempt's stamped deadline is 2027-01-01; this clock is long past it.
_AFTER_STAMPED_DEADLINE = datetime(2028, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize("operation", ["recover", "resolve-conflict"])
def test_reentered_recovering_attempt_reaches_the_authority(
    migrated_url: str, authority_role_dsns: Callable[[str], str], operation: str
) -> None:
    async def main() -> None:
        vehicle = build_vehicle()
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as seed:
            case = await seed_case(
                seed,
                vehicle,
                purpose=operation,
                operation=operation,
                activation_state="recovering",
                attempt_state="recovering",
                with_pre_recovery=operation == "resolve-conflict",
            )
            executor = RecordingExecutor("source")
            ports = ExternalBootHandlerPorts(
                resolver=resolver_for(vehicle),
                incarnation_credential=SecretStr(case.credential),
                secret_registry=SecretRegistry(),
                acknowledger=RecordingAcknowledger(authority_role_dsns("kdive_provider_authority")),
                authority_executor=executor,
                clock=lambda: _AFTER_STAMPED_DEADLINE,
                recovery_readiness_timeout=timedelta(seconds=120),
            )
            handler = build_operations(ports).get(operation)
            assert handler is not None
            job = build_job(
                JobKind.BOOT,
                {"run_id": str(vehicle.run_id), "external_boot_authority_v1": case.marker},
            ).model_copy(update={"id": case.job_id, "attempt": case.attempt})
            async with await role_connection(authority_role_dsns("kdive_worker")) as worker:
                result = await handler(
                    worker, job, ExternalBootAuthorityMarkerV1.model_validate(case.marker)
                )
            assert isinstance(result, ExternalBootAuthoritySuccessV1)
            assert result.result.operation == operation
            assert len(executor.requests) == 1

    asyncio.run(main())
