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
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, LiteralString
from uuid import uuid4

import psycopg
import pytest
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import SecretStr

from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import queue
from kdive.jobs.handlers.external_boot.ports import ExternalBootHandlerPorts
from kdive.jobs.handlers.external_boot.registrar import build_operations
from kdive.jobs.models import (
    ExternalBootAuthorityFailure,
    ExternalBootAuthorityFailureV1,
    ExternalBootAuthorityMarkerV1,
    ExternalBootAuthorityResultV1,
    ExternalBootAuthoritySuccessV1,
    _ActivateResult,
    _FailureResult,
)
from kdive.jobs.worker import _authority_binding_matches
from kdive.providers.external_boot_authority.protocol import (
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
)
from kdive.providers.ports.external_boot import (
    OpaqueProviderRef,
    RecoveryPoint,
)
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


async def _one(conn: AsyncConnection, sql: LiteralString, args: tuple[Any, ...]) -> dict[str, Any]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, args)
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


class _VehicleExecutor:
    def __init__(self, vehicle: Vehicle) -> None:
        self.vehicle = vehicle

    async def execute(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1:
        authority = OpaqueProviderRef(
            ref=f"authority/{request.authority_id}/{request.generation}/{request.attempt_id}"
        )
        operation = request.operation.value
        if operation == "activate":
            self.vehicle.port.activate(self.vehicle.recovery_point, authority)
            self.vehicle.port.observe(self.vehicle.recovery_point, authority)
            category = "target"
        elif operation in {"recover", "resolve-conflict"}:
            self.vehicle.port.recover(self.vehicle.recovery_point, authority)
            self.vehicle.port.observe(self.vehicle.recovery_point, authority)
            category = "source"
        elif operation == "release":
            self.vehicle.port.observe(self.vehicle.recovery_point, authority)
            category = "source"
        else:
            self.vehicle.port.cleanup(self.vehicle.recovery_point, authority)
            category = "absent"
        category = getattr(self.vehicle.port, "authority_category", category)
        return AuthorityObservationV1(
            observation_id=uuid4(), category=category, composite_state="sha256:" + "8" * 64
        )


class _ReceiptExecutor(_VehicleExecutor):
    """Model the authority journal's terminal-observation replay at the handler boundary."""

    def __init__(self, vehicle: Vehicle) -> None:
        super().__init__(vehicle)
        self.observations: dict[str, AuthorityObservationV1] = {}
        self.mutations = 0

    async def execute(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1:
        recorded = self.observations.get(request.operation_identity)
        if recorded is not None:
            return recorded
        if request.operation.value != "release":
            self.mutations += 1
        observation = await super().execute(request)
        self.observations[request.operation_identity] = observation
        return observation


def _ports(
    case: SeededCase, vehicle: Vehicle, dsns: Callable[[str], str]
) -> ExternalBootHandlerPorts:
    return ExternalBootHandlerPorts(
        resolver=resolver_for(vehicle),
        incarnation_credential=SecretStr(case.credential),
        acknowledger=RecordingAcknowledger(dsns("kdive_provider_authority")),
        authority_executor=_VehicleExecutor(vehicle),
    )


async def _durable_rows(conn: AsyncConnection, activation_id: Any) -> tuple[Any, Any]:
    activation = await _one(
        conn,
        "SELECT to_jsonb(a) - 'updated_at' AS value "
        "FROM external_boot_activations AS a WHERE id = %s",
        (activation_id,),
    )
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT to_jsonb(r) - 'updated_at' AS value "
            "FROM external_boot_recovery_attempts AS r WHERE activation_id = %s "
            "ORDER BY attempt_id",
            (activation_id,),
        )
        attempts = [row["value"] for row in await cur.fetchall()]
    return activation["value"], attempts


@pytest.mark.parametrize("operation", ["activate", "release", "recover", "cleanup"])
def test_post_provider_interruption_replays_without_a_second_mutation(
    migrated_url: str,
    authority_role_dsns: Callable[[str], str],
    operation: str,
) -> None:
    """A lost core finalizer converges from the authority receipt exactly once."""
    spec = CASES[operation]

    async def body(seed: AsyncConnection, case: SeededCase) -> None:
        executor = _ReceiptExecutor(case.vehicle)
        ports = ExternalBootHandlerPorts(
            resolver=resolver_for(case.vehicle),
            incarnation_credential=SecretStr(case.credential),
            acknowledger=RecordingAcknowledger(authority_role_dsns("kdive_provider_authority")),
            authority_executor=executor,
        )
        handler = build_operations(ports).get(operation)
        assert handler is not None
        marker = ExternalBootAuthorityMarkerV1.model_validate(case.marker)
        job = _job(case)

        async with await role_connection(authority_role_dsns("kdive_worker")) as worker:
            # The first worker disappears after the provider authority returns.
            await handler(worker, job, marker)
            replayed = await handler(worker, job, marker)
            expected_mutations = 0 if operation == "release" else 1
            assert executor.mutations == expected_mutations
            assert len(executor.observations) == 1
            assert case.vehicle.port.calls == spec["port_calls"]

            committed = await queue.complete_external_boot(
                worker,
                job,
                replayed,
                incarnation_credential=SecretStr(case.credential),
            )
            assert committed is not None
            before = await _durable_rows(seed, case.vehicle.activation_id)

            # Exact finalizer replay must not duplicate durable effects or leak a transition.
            await queue.complete_external_boot(
                worker,
                job,
                replayed,
                incarnation_credential=SecretStr(case.credential),
            )
            after = await _durable_rows(seed, case.vehicle.activation_id)

        assert before == after
        assert executor.mutations == expected_mutations
        assert before[0]["state"] == spec["after"]
        if operation == "release":
            row = await _one(
                seed,
                "SELECT count(*) AS count FROM external_boot_reservation_releases "
                "WHERE activation_id = %s",
                (case.vehicle.activation_id,),
            )
            assert row["count"] == 1

    _drive(migrated_url, authority_role_dsns, operation, body)


async def _run_operation(
    dsns: Callable[[str], str], seed: AsyncConnection, case: SeededCase, operation: str
) -> ExternalBootAuthoritySuccessV1:
    ports = _ports(case, case.vehicle, dsns)
    operations = build_operations(ports)
    handler = operations.get(operation)
    assert handler is not None
    async with await role_connection(dsns("kdive_worker")) as worker:
        result = await handler(
            worker, _job(case), ExternalBootAuthorityMarkerV1.model_validate(case.marker)
        )
        if result.result.operation == "recovery-attempt":
            committed = await queue.complete_external_boot(
                worker,
                _job(case),
                result,
                incarnation_credential=SecretStr(case.credential),
            )
            assert committed is not None
            return await handler(
                worker, _job(case), ExternalBootAuthorityMarkerV1.model_validate(case.marker)
            )
        return result
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

        # 3. criterion 5. The result is passed to the commit **as the handler returned it**, not
        # re-validated into the success subclass first: `_commit_external_result` dispatches on
        # isinstance, and a bare ExternalBootAuthorityResultV1 is logged as an "untyped result
        # variant" and written nowhere. Converting here would make this test more permissive than
        # the worker and hide exactly that — which it did, until the end-to-end test caught it.
        assert isinstance(result, ExternalBootAuthoritySuccessV1)
        async with await role_connection(authority_role_dsns("kdive_worker")) as worker:
            committed = await queue.complete_external_boot(
                worker,
                _job(case),
                result,
                incarnation_credential=SecretStr(case.credential),
            )
        assert committed is not None
        row = await _activation_row(seed, case.vehicle.activation_id)
        assert row["state"] == spec["after"]

        # 4. criterion 6, applied half
        assert await _job_state(seed, case.job_id) == "succeeded"

    _drive(migrated_url, authority_role_dsns, operation, body)


def test_activate_reuses_the_persisted_readiness_deadline(
    migrated_url: str, authority_role_dsns: Callable[[str], str]
) -> None:
    """Retries carry forward the row's absolute deadline instead of extending it."""

    async def body(seed: AsyncConnection, case: SeededCase) -> None:
        result = await _run_operation(authority_role_dsns, seed, case, "activate")

        payload = result.result
        assert isinstance(payload, _ActivateResult)
        assert payload.activation_readiness_deadline == datetime(2027, 1, 1, tzinfo=UTC)

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
                result,
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
                result,
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


@pytest.mark.parametrize("operation", ["activate", "recover", "resolve-conflict", "release"])
def test_a_disagreeing_kernel_observation_refuses_to_emit_terminal_evidence(
    migrated_url: str, authority_role_dsns: Callable[[str], str], operation: str
) -> None:
    """A non-terminal authority category cannot be promoted to lifecycle evidence."""
    spec = CASES[operation]

    async def body(seed: AsyncConnection, case: SeededCase) -> None:
        case.vehicle.port.__dict__["authority_category"] = "conflict"

        with pytest.raises(ExternalBootAuthorityFailure) as excinfo:
            await _run_operation(authority_role_dsns, seed, case, operation)

        payload = excinfo.value.result.result
        assert isinstance(payload, _FailureResult)
        assert payload.failure_context.phase == "commit"
        # The mutation happened; what is refused is *recording* it as a good terminal state.
        row = await _activation_row(seed, case.vehicle.activation_id)
        expected_state = (
            "recovering" if operation == "resolve-conflict" else spec["activation_state"]
        )
        assert row["state"] == expected_state

    _drive(migrated_url, authority_role_dsns, operation, body)


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


def test_an_authority_superseded_commit_is_classified_and_leaves_the_job_running(
    migrated_url: str, authority_role_dsns: Callable[[str], str]
) -> None:
    """The persistence seam classifies authority loss for the worker finalizer.

    The queue layer deliberately leaves consumption to the worker, so this direct persistence test
    also observes the unchanged running row.
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

        assert committed is queue.ExternalBootCommitStatus.AUTHORITY_SUPERSEDED
        assert await _job_state(seed, case.job_id) == "running"

    _drive(migrated_url, authority_role_dsns, "activate", body)


def test_the_persisted_teardown_identity_digests_the_persisted_teardown_evidence(
    migrated_url: str, authority_role_dsns: Callable[[str], str]
) -> None:
    """``teardown_identity`` must name a document an auditor can recompute from the stored row.

    The commit persists ``teardown_evidence`` verbatim (``0122…sql:1454-1458``) and checks only
    that ``teardown_identity`` *looks like* a sha256 — it never recomputes it. So a handler
    digesting a different document produces an identity that names nothing, and no schema check
    would ever catch it. An earlier version of this handler digested
    ``{schema, system_id, system_state, generation}`` while emitting
    ``{schema, system_id, system_state, observed_at}``.

    This reads both columns back out of Postgres and recomputes the digest exactly as an auditor
    would, rather than comparing two values the handler produced.
    """

    async def body(seed: AsyncConnection, case: SeededCase) -> None:
        result = await _run_operation(authority_role_dsns, seed, case, "teardown")
        async with await role_connection(authority_role_dsns("kdive_worker")) as worker:
            committed = await queue.complete_external_boot(
                worker, _job(case), result, incarnation_credential=SecretStr(case.credential)
            )
        assert committed is not None

        row = await _one(
            seed,
            "SELECT teardown_evidence, cleanup_evidence FROM external_boot_activations "
            "WHERE id = %s",
            (case.vehicle.activation_id,),
        )
        recomputed = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(row["teardown_evidence"], sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )

        assert row["cleanup_evidence"]["teardown_identity"] == recomputed

    _drive(migrated_url, authority_role_dsns, "teardown", body)
