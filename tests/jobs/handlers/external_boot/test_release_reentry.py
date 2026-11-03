"""A re-entered derived release recovery starts its own readiness budget (#3395, ADR-0684)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import psycopg
import pytest
from pydantic import SecretStr

from kdive.domain.capacity.state import ExternalBootActivationState as State
from kdive.domain.operations.jobs import JobKind
from kdive.jobs.handlers.external_boot import lifecycle
from kdive.jobs.handlers.external_boot.ports import ExternalBootHandlerPorts
from kdive.jobs.handlers.external_boot.registrar import build_operations
from kdive.jobs.handlers.external_boot.runner import OperationContext
from kdive.jobs.models import ExternalBootAuthorityFailure, ExternalBootAuthorityMarkerV1
from kdive.providers.external_boot_authority.protocol import (
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.jobs.handlers.external_boot.conftest import resolver_for, role_connection
from tests.jobs.handlers.external_boot.seeding import RecordingAcknowledger, seed_case
from tests.jobs.handlers.external_boot.support import build_job
from tests.jobs.handlers.external_boot.vehicle import build_vehicle

_NOW = datetime.now(UTC)
_TIMEOUT = timedelta(seconds=120)
_REQUEST_DEADLINE = _NOW + timedelta(hours=1)


class _NeverExecuted:
    async def execute(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1:
        raise AssertionError("the begin step stops the handler before any mutation")


class _Stop(Exception):
    """Raised once ``begin_external_boot_derived_release_recovery`` has been asked."""


@pytest.mark.parametrize(
    ("job_attempt", "expected"),
    [
        pytest.param(1, _REQUEST_DEADLINE, id="first-attempt-keeps-the-request-deadline"),
        pytest.param(2, _NOW + _TIMEOUT, id="a-later-attempt-gets-a-fresh-deadline"),
    ],
)
def test_release_begin_deadline(
    migrated_url: str,
    authority_role_dsns: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
    job_attempt: int,
    expected: datetime,
) -> None:
    seen: list[Any] = []

    async def capture(_connection: object, _sql: str, args: tuple[Any, ...]) -> str:
        seen.append(args[6])
        raise _Stop

    monkeypatch.setattr(lifecycle, "_derived_release_status", capture)

    async def main() -> None:
        vehicle = build_vehicle()
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as seed:
            case = await seed_case(
                seed,
                vehicle,
                purpose="release",
                operation="release",
                activation_state="active",
                with_reservation=True,
            )
            await seed.execute(
                "UPDATE jobs SET attempt = %s WHERE id = %s", (job_attempt, case.job_id)
            )
            ports = ExternalBootHandlerPorts(
                resolver=resolver_for(vehicle),
                incarnation_credential=SecretStr(case.credential),
                secret_registry=SecretRegistry(),
                acknowledger=RecordingAcknowledger(authority_role_dsns("kdive_provider_authority")),
                authority_executor=_NeverExecuted(),
                clock=lambda: _NOW,
                recovery_readiness_timeout=_TIMEOUT,
            )
            handler = build_operations(ports).get("release")
            assert handler is not None
            job = build_job(
                JobKind.BOOT,
                {
                    "run_id": str(vehicle.run_id),
                    "external_boot_authority_v1": case.marker,
                    "recovery_request_v1": {
                        "request_identity": "sha256:" + "1" * 64,
                        "readiness_deadline": _REQUEST_DEADLINE.isoformat(),
                    },
                },
            ).model_copy(update={"id": case.job_id, "attempt": job_attempt})
            async with await role_connection(authority_role_dsns("kdive_worker")) as worker:
                with pytest.raises(ExternalBootAuthorityFailure):
                    await handler(
                        worker, job, ExternalBootAuthorityMarkerV1.model_validate(case.marker)
                    )

    asyncio.run(main())
    assert seen == [expected]


@pytest.mark.parametrize(
    ("job_attempt", "activation_state", "fresh"),
    [
        (1, State.ACTIVE, False),
        (1, State.RECOVERED, False),
        (2, State.ACTIVE, True),
        (3, State.RECOVERED, True),
        (1, State.RECOVERING, True),
        (2, State.RECOVERING, True),
    ],
)
def test_release_re_entry_predicate(job_attempt: int, activation_state: State, fresh: bool) -> None:
    context = SimpleNamespace(
        job=SimpleNamespace(attempt=job_attempt), activation=SimpleNamespace(state=activation_state)
    )
    assert lifecycle._starts_fresh_readiness_budget(cast(OperationContext, context)) is fresh
