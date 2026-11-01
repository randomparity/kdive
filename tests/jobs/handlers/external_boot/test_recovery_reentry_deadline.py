"""Recover and resolve-conflict pre-checks give a re-entry a fresh deadline (#3465)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from pydantic import SecretStr

from kdive.domain.errors import ErrorCategory
from kdive.domain.operations.jobs import JobKind
from kdive.jobs.handlers.external_boot.ports import ExternalBootHandlerPorts
from kdive.jobs.handlers.external_boot.registrar import build_operations
from kdive.jobs.models import (
    ExternalBootAuthorityConflictV1,
    ExternalBootAuthorityFailure,
    ExternalBootAuthorityMarkerV1,
    ExternalBootAuthoritySuccessV1,
    _FailureResult,
    _RecoveryAttemptResult,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.jobs.handlers.external_boot.conftest import resolver_for, role_connection
from tests.jobs.handlers.external_boot.seeding import RecordingAcknowledger, seed_case
from tests.jobs.handlers.external_boot.support import CASES, build_job
from tests.jobs.handlers.external_boot.test_lifecycle import _VehicleExecutor
from tests.jobs.handlers.external_boot.vehicle import build_vehicle

_NOW = datetime(2026, 11, 1, tzinfo=UTC)
_TIMEOUT = timedelta(seconds=120)
_EXPIRED = _NOW - timedelta(minutes=10)
# The recover case is seeded mid-recovery; the pre-check only runs from ``active``.
_FIRST_ENTRY_STATE = {"recover": "active"}


def _run(
    migrated_url: str, dsns: Callable[[str], str], operation: str, job_attempt: int
) -> ExternalBootAuthoritySuccessV1 | ExternalBootAuthorityConflictV1:
    async def main() -> ExternalBootAuthoritySuccessV1 | ExternalBootAuthorityConflictV1:
        spec = CASES[operation]
        vehicle = build_vehicle()
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as seed:
            case = await seed_case(
                seed,
                vehicle,
                purpose=spec["purpose"],
                operation=operation,
                activation_state=_FIRST_ENTRY_STATE.get(operation, spec["activation_state"]),
                **spec["seed"],
            )
            await seed.execute(
                "UPDATE jobs SET attempt = %s WHERE id = %s", (job_attempt, case.job_id)
            )
            ports = ExternalBootHandlerPorts(
                resolver=resolver_for(vehicle),
                incarnation_credential=SecretStr(case.credential),
                secret_registry=SecretRegistry(),
                acknowledger=RecordingAcknowledger(dsns("kdive_provider_authority")),
                authority_executor=_VehicleExecutor(vehicle),
                clock=lambda: _NOW,
                recovery_readiness_timeout=_TIMEOUT,
            )
            handler = build_operations(ports).get(operation)
            assert handler is not None
            job = build_job(
                JobKind.BOOT,
                {
                    "run_id": str(vehicle.run_id),
                    "external_boot_authority_v1": case.marker,
                    "recovery_request_v1": {
                        "request_identity": "sha256:" + "1" * 64,
                        "readiness_deadline": _EXPIRED.isoformat(),
                    },
                },
            ).model_copy(update={"id": case.job_id, "attempt": job_attempt})
            async with await role_connection(dsns("kdive_worker")) as worker:
                return await handler(
                    worker, job, ExternalBootAuthorityMarkerV1.model_validate(case.marker)
                )

    return asyncio.run(main())


@pytest.mark.parametrize("operation", ["recover", "resolve-conflict"])
def test_reentry_gets_a_fresh_deadline_not_a_refusal(
    migrated_url: str, authority_role_dsns: Callable[[str], str], operation: str
) -> None:
    result = _run(migrated_url, authority_role_dsns, operation, job_attempt=2)

    assert isinstance(result, ExternalBootAuthoritySuccessV1)
    assert isinstance(result.result, _RecoveryAttemptResult)
    assert result.result.deadline == _NOW + _TIMEOUT


@pytest.mark.parametrize("operation", ["recover", "resolve-conflict"])
def test_first_entry_still_refuses_an_expired_deadline(
    migrated_url: str, authority_role_dsns: Callable[[str], str], operation: str
) -> None:
    with pytest.raises(ExternalBootAuthorityFailure) as raised:
        _run(migrated_url, authority_role_dsns, operation, job_attempt=1)

    failure = raised.value.result.result
    assert isinstance(failure, _FailureResult)
    assert failure.error_category == ErrorCategory.READINESS_FAILURE.value
