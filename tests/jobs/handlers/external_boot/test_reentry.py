"""Worker external-boot operations resume from authority-owned observations (ADR-0595)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Literal
from uuid import uuid4

import psycopg
import pytest
from pydantic import SecretStr

from kdive.domain.operations.jobs import JobKind
from kdive.jobs.handlers.external_boot.ports import ExternalBootHandlerPorts
from kdive.jobs.handlers.external_boot.registrar import build_operations
from kdive.jobs.models import ExternalBootAuthorityMarkerV1
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
