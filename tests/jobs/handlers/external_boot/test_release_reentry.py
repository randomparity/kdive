"""A re-entered derived release recovery starts its own readiness budget (#3395, ADR-0684)."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest
from pydantic import SecretStr

from kdive.domain.capacity.state import ExternalBootActivationState as State
from kdive.domain.errors import ErrorCategory
from kdive.domain.operations.jobs import JobKind
from kdive.jobs.external_boot_models import (
    ExternalBootAuthorityConflictV1,
    ExternalBootAuthorityFailure,
    ExternalBootAuthorityMarkerV1,
    ExternalBootDerivedReleaseCompletion,
)
from kdive.jobs.handlers.external_boot import lifecycle
from kdive.jobs.handlers.external_boot.ports import ExternalBootHandlerPorts
from kdive.jobs.handlers.external_boot.registrar import build_operations
from kdive.jobs.handlers.external_boot.runner import OperationContext
from kdive.providers.external_boot_authority.protocol import (
    AuthorityConflictResolutionRequestV1,
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

    async def execute_conflict_resolution(
        self, request: AuthorityConflictResolutionRequestV1
    ) -> AuthorityObservationV1:
        raise AssertionError("the begin step stops the handler before conflict resolution")

    async def observe(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1:
        raise AssertionError("the begin step stops the handler before observation")


class _Stop(Exception):
    """Raised once ``begin_external_boot_derived_release_recovery`` has been asked."""


@pytest.mark.parametrize(
    ("job_attempt", "expected"),
    [
        pytest.param(1, _REQUEST_DEADLINE, id="first-attempt-keeps-the-request-deadline"),
        pytest.param(2, _NOW + _TIMEOUT, id="a-later-attempt-gets-a-fresh-deadline"),
    ],
)
@pytest.mark.parametrize("factory_selected", [False, True])
def test_release_begin_deadline(
    migrated_url: str,
    authority_role_dsns: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
    job_attempt: int,
    expected: datetime,
    factory_selected: bool,
) -> None:
    seen: list[Any] = []
    contexts: list[OperationContext] = []
    selected: list[object] = []
    original = lifecycle._run_active_release

    async def capture_context(context: OperationContext, **kwargs: Any) -> Any:
        contexts.append(context)
        return await original(context, **kwargs)

    monkeypatch.setattr(lifecycle, "_run_active_release", capture_context)

    async def capture(_connection: object, _sql: str, args: tuple[Any, ...]) -> str:
        context = contexts[-1]
        assert _connection is context.connection
        assert (
            args[0]
            == hashlib.sha256(context.incarnation_credential.get_secret_value().encode()).digest()
        )
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
            executor = _NeverExecuted()
            acknowledger = RecordingAcknowledger(authority_role_dsns("kdive_provider_authority"))

            class Client(_NeverExecuted):
                acknowledge = acknowledger.acknowledge

            client = Client()
            selected.append(client if factory_selected else executor)
            ports = ExternalBootHandlerPorts(
                resolver=resolver_for(vehicle),
                incarnation_credential=SecretStr(case.credential),
                secret_registry=SecretRegistry(),
                acknowledger=acknowledger,
                authority_executor=executor,
                authority_client_factory=(
                    (lambda _binding, _marker, _deadline: cast(Any, client))
                    if factory_selected
                    else None
                ),
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
    assert len(contexts) == 1
    assert contexts[0].authority_executor is selected[0]
    assert (
        not {"authority_executor", "connection", "incarnation_credential", "deadline"}
        & contexts[0].prerequisites.keys()
    )


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


@pytest.mark.parametrize(
    ("activation_state", "stop_at", "recover_category", "adopted"),
    [
        ("active", None, "source", True),
        ("recovered", None, "source", False),
        ("active", None, "conflict", True),
        ("active", "begin_external_boot_derived_release_recovery", "source", True),
        ("active", "commit_external_boot_derived_release_recovery", "source", True),
        ("recovered", "adopt_external_boot_release_cleanup_receipt_from_head", "source", True),
        ("recovered", "record_external_boot_release_cleanup_receipt_from_head", "source", False),
        ("recovered", "finalize_external_boot_derived_release", "source", True),
    ],
)
def test_release_context_routes_later_phases(
    migrated_url: str,
    authority_role_dsns: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
    activation_state: str,
    stop_at: str | None,
    recover_category: str,
    adopted: bool,
) -> None:
    """Real admission, with deterministic authority/SQL boundaries for each release branch."""
    contexts: list[OperationContext] = []
    phases: list[str] = []
    mutations: list[str] = []
    original = lifecycle._run_active_release

    class Executor(_NeverExecuted):
        async def execute(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1:
            mutations.append(request.operation.value)
            return AuthorityObservationV1.model_validate(
                {
                    "observation_id": uuid4(),
                    "category": (
                        recover_category if request.operation.value == "recover" else "absent"
                    ),
                    "composite_state": "sha256:" + "8" * 64,
                }
            )

    async def capture_context(context: OperationContext, **kwargs: Any) -> Any:
        contexts.append(context)
        return await original(context, **kwargs)

    async def status(connection: object, sql: str, args: tuple[Any, ...]) -> str:
        context = contexts[-1]
        assert connection is context.connection
        assert args[:5] == (
            hashlib.sha256(context.incarnation_credential.get_secret_value().encode()).digest(),
            context.job.id,
            context.job.attempt,
            context.authority.authority_id,
            context.authority.generation,
        )
        phase = sql.split("public.")[1].split("(")[0]
        phases.append(phase)
        if phase == stop_at:
            return "superseded"
        if phase == "adopt_external_boot_release_cleanup_receipt_from_head" and not adopted:
            return "not_applicable"
        return "applied"

    monkeypatch.setattr(lifecycle, "_run_active_release", capture_context)
    monkeypatch.setattr(lifecycle, "_derived_release_status", status)

    async def run() -> None:
        vehicle = build_vehicle()
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as seed:
            case = await seed_case(
                seed,
                vehicle,
                purpose="release",
                operation="release",
                activation_state=activation_state,
                with_reservation=True,
            )
            handler = build_operations(
                ExternalBootHandlerPorts(
                    resolver=resolver_for(vehicle),
                    incarnation_credential=SecretStr(case.credential),
                    secret_registry=SecretRegistry(),
                    authority_executor=Executor(),
                    acknowledger=RecordingAcknowledger(
                        authority_role_dsns("kdive_provider_authority")
                    ),
                )
            ).get("release")
            assert handler is not None
            job = build_job(
                JobKind.BOOT,
                {
                    "run_id": str(vehicle.run_id),
                    "external_boot_authority_v1": case.marker,
                },
            ).model_copy(update={"id": case.job_id, "attempt": 1})
            marker = ExternalBootAuthorityMarkerV1.model_validate(case.marker)
            async with await role_connection(authority_role_dsns("kdive_worker")) as worker:
                if stop_at is not None:
                    with pytest.raises(ExternalBootAuthorityFailure) as raised:
                        await handler(worker, job, marker)
                    assert raised.value.category is ErrorCategory.STALE_HANDLE
                    assert (
                        raised.value.result.model_dump(mode="json")["result"]["failure_context"][
                            "phase"
                        ]
                        == "commit"
                    )
                else:
                    result = await handler(worker, job, marker)
                    expected = (
                        ExternalBootAuthorityConflictV1
                        if recover_category == "conflict"
                        else ExternalBootDerivedReleaseCompletion
                    )
                    assert isinstance(result, expected)

    asyncio.run(run())
    expected_phases = (
        []
        if activation_state == "recovered"
        else [
            "begin_external_boot_derived_release_recovery",
            "commit_external_boot_derived_release_recovery",
        ]
    ) + ["adopt_external_boot_release_cleanup_receipt_from_head"]
    if not adopted:
        expected_phases.append("record_external_boot_release_cleanup_receipt_from_head")
    expected_phases.append("finalize_external_boot_derived_release")
    if recover_category == "conflict":
        expected_phases = expected_phases[:1]
    elif stop_at is not None:
        expected_phases = expected_phases[: expected_phases.index(stop_at) + 1]
    assert phases == expected_phases
    expected_mutations = [] if activation_state == "recovered" else ["recover"]
    if (
        stop_at
        not in {
            "begin_external_boot_derived_release_recovery",
            "commit_external_boot_derived_release_recovery",
        }
        and recover_category != "conflict"
    ):
        expected_mutations.append("cleanup")
    if stop_at == "begin_external_boot_derived_release_recovery":
        expected_mutations = []
    assert mutations == expected_mutations
