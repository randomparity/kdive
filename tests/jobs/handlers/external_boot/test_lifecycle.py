"""Each of the six operations, driven end to end against a real Postgres and the fault-inject port.

Charter criteria 5, 6 and 7. Every operation asserts the same four things:

1. the port method the specification's §7 table names was called, with the **persisted** recovery
   point — compared against the row read back out of the database, not against the object the test
   handed the seeder, so a handler that used its own copy would fail;
2. the returned result passes ``kdive.jobs.worker._authority_binding_matches`` against the payload
   marker, driven through that function rather than a re-implementation of its nine comparisons;
3. committing through ``queue.complete_external_boot`` returns a ``Job`` and leaves the activation
   row in the state the §7 table records;
4. the ``jobs`` row afterwards is ``succeeded``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import SecretStr

from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import queue
from kdive.jobs.handlers.external_boot.lifecycle import ACTIVATION_READINESS_WINDOW
from kdive.jobs.handlers.external_boot.ports import ExternalBootHandlerPorts
from kdive.jobs.handlers.external_boot.registrar import build_operations
from kdive.jobs.models import (
    ExternalBootAuthorityFailureV1,
    ExternalBootAuthorityMarkerV1,
    ExternalBootAuthorityResultV1,
    ExternalBootAuthoritySuccessV1,
    _ActivateResult,
)
from kdive.jobs.worker import _authority_binding_matches
from kdive.providers.ports.external_boot import RecoveryPoint
from tests.jobs.handlers.external_boot.conftest import resolver_for, role_connection
from tests.jobs.handlers.external_boot.seeding import RecordingAcknowledger, SeededCase, seed_case
from tests.jobs.handlers.external_boot.support import CASES, build_job
from tests.jobs.handlers.external_boot.vehicle import Vehicle, build_vehicle


def _job(case: SeededCase) -> Job:
    kind = JobKind.TEARDOWN if case.purpose == "teardown" else JobKind.BOOT
    key = "system_id" if kind is JobKind.TEARDOWN else "run_id"
    value = case.vehicle.system_id if kind is JobKind.TEARDOWN else case.vehicle.run_id
    return build_job(kind, {key: str(value), "external_boot_authority_v1": case.marker}).model_copy(
        update={"id": case.job_id, "attempt": case.attempt}
    )


async def _persisted_recovery_point(conn: AsyncConnection, activation_id: Any) -> RecoveryPoint:
    """Read the recovery point back out of Postgres, so the comparison is against the row."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT recovery_point FROM external_boot_activations WHERE id = %s", (activation_id,)
        )
        row = await cur.fetchone()
    assert row is not None
    return RecoveryPoint.model_validate(row["recovery_point"])


async def _activation_row(conn: AsyncConnection, activation_id: Any) -> dict[str, Any]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT state, cleanup_complete FROM external_boot_activations WHERE id = %s",
            (activation_id,),
        )
        row = await cur.fetchone()
    assert row is not None
    return dict(row)


async def _job_state(conn: AsyncConnection, job_id: Any) -> str:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT state FROM jobs WHERE id = %s", (job_id,))
        row = await cur.fetchone()
    assert row is not None
    return str(row["state"])


async def _system_state(conn: AsyncConnection, system_id: Any) -> str:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT state FROM systems WHERE id = %s", (system_id,))
        row = await cur.fetchone()
    assert row is not None
    return str(row["state"])


def _ports(
    case: SeededCase, vehicle: Vehicle, dsns: Callable[[str], str]
) -> ExternalBootHandlerPorts:
    return ExternalBootHandlerPorts(
        resolver=resolver_for(vehicle),
        incarnation_credential=SecretStr(case.credential),
        acknowledger=RecordingAcknowledger(dsns("kdive_provider_authority")),
    )


async def _run_operation(
    dsns: Callable[[str], str], seed: AsyncConnection, case: SeededCase, operation: str
) -> ExternalBootAuthorityResultV1:
    ports = _ports(case, case.vehicle, dsns)
    operations = build_operations(ports)
    handler = operations.get(operation)
    assert handler is not None
    async with await role_connection(dsns("kdive_worker")) as worker:
        return await handler(
            worker, _job(case), ExternalBootAuthorityMarkerV1.model_validate(case.marker)
        )
    del seed


def _drive(
    migrated_url: str,
    dsns: Callable[[str], str],
    operation: str,
    body: Callable[[AsyncConnection, SeededCase], Awaitable[None]],
) -> None:
    spec = CASES[operation]

    async def _main() -> None:
        vehicle = build_vehicle()
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as seed:
            case = await seed_case(
                seed,
                vehicle,
                purpose=spec["purpose"],
                operation=operation,
                activation_state=spec["activation_state"],
                **spec["seed"],
            )
            await body(seed, case)

    asyncio.run(_main())


@pytest.mark.parametrize("operation", list(CASES))
def test_operation_calls_its_port_commits_and_leaves_the_job_succeeded(
    migrated_url: str, authority_role_dsns: Callable[[str], str], operation: str
) -> None:
    spec = CASES[operation]

    async def body(seed: AsyncConnection, case: SeededCase) -> None:
        persisted = await _persisted_recovery_point(seed, case.vehicle.activation_id)

        result = await _run_operation(authority_role_dsns, seed, case, operation)

        # 1. the §7 port call, against the row's recovery point rather than the test's object
        assert case.vehicle.port.calls == spec["port_calls"]
        assert case.vehicle.port.recoveries[0] == persisted

        # 2. criterion 7, through the worker's own gate
        marker = ExternalBootAuthorityMarkerV1.model_validate(case.marker)
        assert _authority_binding_matches(marker, result) is True

        # 3. criterion 5
        async with await role_connection(authority_role_dsns("kdive_worker")) as worker:
            committed = await queue.complete_external_boot(
                worker,
                _job(case),
                ExternalBootAuthoritySuccessV1.model_validate(result.model_dump(by_alias=True)),
                incarnation_credential=SecretStr(case.credential),
            )
        assert committed is not None
        row = await _activation_row(seed, case.vehicle.activation_id)
        assert row["state"] == spec["after"]

        # 4. criterion 6, applied half
        assert await _job_state(seed, case.job_id) == "succeeded"

    _drive(migrated_url, authority_role_dsns, operation, body)


def test_activate_emits_a_readiness_deadline_one_window_ahead(
    migrated_url: str, authority_role_dsns: Callable[[str], str]
) -> None:
    """``_ActivateResult.activation_readiness_deadline`` is required, so a value must be emitted.

    Nothing reads it today — the commit stores it after a parse check only and no reader exists in
    ``src/`` outside the model definition — so this asserts the unit and reference clock the
    docstring claims (``now(UTC)`` plus ``ACTIVATION_READINESS_WINDOW``) and nothing more.
    Enforcing the deadline is #2202's.
    """

    async def body(seed: AsyncConnection, case: SeededCase) -> None:
        before = datetime.now(UTC)

        result = await _run_operation(authority_role_dsns, seed, case, "activate")

        payload = result.result
        assert isinstance(payload, _ActivateResult)
        assert before + ACTIVATION_READINESS_WINDOW <= payload.activation_readiness_deadline
        assert payload.activation_readiness_deadline <= (
            datetime.now(UTC) + ACTIVATION_READINESS_WINDOW
        )

    _drive(migrated_url, authority_role_dsns, "activate", body)


def test_cleanup_marks_the_activation_cleanup_complete(
    migrated_url: str, authority_role_dsns: Callable[[str], str]
) -> None:
    async def body(seed: AsyncConnection, case: SeededCase) -> None:
        result = await _run_operation(authority_role_dsns, seed, case, "cleanup")
        async with await role_connection(authority_role_dsns("kdive_worker")) as worker:
            await queue.complete_external_boot(
                worker,
                _job(case),
                ExternalBootAuthoritySuccessV1.model_validate(result.model_dump(by_alias=True)),
                incarnation_credential=SecretStr(case.credential),
            )

        assert (await _activation_row(seed, case.vehicle.activation_id))["cleanup_complete"] is True

    _drive(migrated_url, authority_role_dsns, "cleanup", body)


def test_teardown_drives_the_system_to_torn_down(
    migrated_url: str, authority_role_dsns: Callable[[str], str]
) -> None:
    async def body(seed: AsyncConnection, case: SeededCase) -> None:
        result = await _run_operation(authority_role_dsns, seed, case, "teardown")
        async with await role_connection(authority_role_dsns("kdive_worker")) as worker:
            await queue.complete_external_boot(
                worker,
                _job(case),
                ExternalBootAuthoritySuccessV1.model_validate(result.model_dump(by_alias=True)),
                incarnation_credential=SecretStr(case.credential),
            )

        assert await _system_state(seed, case.vehicle.system_id) == "torn_down"
        assert (await _activation_row(seed, case.vehicle.activation_id))["cleanup_complete"] is True

    _drive(migrated_url, authority_role_dsns, "teardown", body)


@pytest.mark.parametrize(
    "field", ["activation_id", "run_id", "system_id", "plan_identity", "operation_identity"]
)
def test_a_mismatched_result_is_rejected_before_the_commit(
    migrated_url: str, authority_role_dsns: Callable[[str], str], field: str
) -> None:
    """Criterion 7's second half: the worker's gate refuses, so the commit is never attempted."""

    async def body(seed: AsyncConnection, case: SeededCase) -> None:
        result = await _run_operation(authority_role_dsns, seed, case, "activate")
        marker = ExternalBootAuthorityMarkerV1.model_validate(case.marker)
        mutated = result.model_copy(update={field: _other(field, result)})

        assert _authority_binding_matches(marker, result) is True
        assert _authority_binding_matches(marker, mutated) is False

    _drive(migrated_url, authority_role_dsns, "activate", body)


def _other(field: str, result: ExternalBootAuthorityResultV1) -> Any:
    from uuid import uuid4

    if field == "plan_identity":
        return "sha256:" + "9" * 64
    if field == "operation_identity":
        return f"not-{result.operation_identity}"
    return uuid4()


def test_a_terminal_failure_result_leaves_the_job_failed(
    migrated_url: str, authority_role_dsns: Callable[[str], str]
) -> None:
    """Criterion 6's not-applied half. Neither arm leaves the ``jobs`` row ``running``."""

    async def body(seed: AsyncConnection, case: SeededCase) -> None:
        result = await _run_operation(authority_role_dsns, seed, case, "activate")
        # `admitted_operation` stays the admission the job was claimed under, not the commit point:
        # the commit re-checks it against both the authority row's `operation` and the payload
        # marker's, and `_authority_binding_matches` compares it to `marker.operation`. Only the
        # nested result payload's `operation` becomes `fail`.
        failure = ExternalBootAuthorityFailureV1.model_validate(
            result.model_dump(by_alias=True)
            | {
                "result": {
                    "schema": "external-boot-authority-result-v1",
                    "operation": "fail",
                    "error_category": "infrastructure_failure",
                    "failure_context": {"phase": "provider-call"},
                    "terminal": True,
                },
            }
        )

        async with await role_connection(authority_role_dsns("kdive_worker")) as worker:
            committed = await queue.fail_external_boot(
                worker,
                _job(case),
                failure,
                incarnation_credential=SecretStr(case.credential),
            )

        assert committed is not None
        assert await _job_state(seed, case.job_id) == "failed"

    _drive(migrated_url, authority_role_dsns, "activate", body)


def test_a_superseded_commit_leaves_the_job_running(
    migrated_url: str, authority_role_dsns: Callable[[str], str]
) -> None:
    """Records the #2203-owned availability leak; it does not fix it.

    A commit that returns ``superseded`` writes no ``jobs`` row at all, so the job keeps its lease,
    is re-claimed when it lapses, and once ``attempt >= max_attempts`` is permanently ``running`` —
    both generic finalizers and ``repair_abandoned_jobs`` are fenced against a marked payload.
    Reaping it is #2203's and re-entry is #2202's; this test pins the behaviour so it cannot change
    silently, and is not coverage of an intended outcome.
    """

    async def body(seed: AsyncConnection, case: SeededCase) -> None:
        result = await _run_operation(authority_role_dsns, seed, case, "activate")
        # A generation the authority row does not hold makes the commit's binding re-check fail.
        stale = ExternalBootAuthoritySuccessV1.model_validate(
            result.model_dump(by_alias=True) | {"generation": result.generation + 1}
        )

        async with await role_connection(authority_role_dsns("kdive_worker")) as worker:
            committed = await queue.complete_external_boot(
                worker, _job(case), stale, incarnation_credential=SecretStr(case.credential)
            )

        assert committed is None
        assert await _job_state(seed, case.job_id) == "running"

    _drive(migrated_url, authority_role_dsns, "activate", body)
