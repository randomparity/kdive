"""The shared runner's admission order, against a real Postgres (spec §6).

Every refusal these tests assert happens **before** allocation, and each asserts that positively —
by counting ``external_boot_authorities`` rows afterwards — rather than by the exception alone. The
distinction is load-bearing, not pedantry: a refusal after allocation, like a ``superseded``
allocation, cannot be committed as a failure, so the job keeps its lease, is re-claimed, burns an
attempt, and eventually wedges ``running``. An exception-only assertion would pass either way.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import SecretStr

from kdive.domain.capacity.state import ExternalBootActivationState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.external_boot_activation import ExternalBootActivation
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.handlers.external_boot.ports import ExternalBootHandlerPorts
from kdive.jobs.handlers.external_boot.runner import OperationContext, authority_ref, run_operation
from kdive.jobs.models import (
    ExternalBootAuthorityFailure,
    ExternalBootAuthorityMarkerV1,
    ExternalBootAuthorityResultV1,
    _FailureResult,
)
from kdive.jobs.worker import _authority_binding_matches
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.ports.external_boot import RunningKernelObservation
from tests.jobs.handlers.external_boot.conftest import resolver_for, role_connection
from tests.jobs.handlers.external_boot.seeding import RecordingAcknowledger, SeededCase, seed_case
from tests.jobs.handlers.external_boot.support import build_job
from tests.jobs.handlers.external_boot.vehicle import Vehicle
from tests.mcp.systems_support import provider_resolver

ACTIVATING = frozenset({ExternalBootActivationState.ACTIVATING})
NO_EVIDENCE: frozenset[str] = frozenset()


async def _no_preconditions(
    _conn: AsyncConnection,
    _activation: ExternalBootActivation,
    _marker: ExternalBootAuthorityMarkerV1,
) -> None:
    return None


def _marker(case: SeededCase, **overrides: Any) -> ExternalBootAuthorityMarkerV1:
    return ExternalBootAuthorityMarkerV1.model_validate(case.marker | overrides)


def _job(case: SeededCase) -> Job:
    kind = JobKind.TEARDOWN if case.purpose == "teardown" else JobKind.BOOT
    key = "system_id" if kind is JobKind.TEARDOWN else "run_id"
    value = case.vehicle.system_id if kind is JobKind.TEARDOWN else case.vehicle.run_id
    job = build_job(kind, {key: str(value), "external_boot_authority_v1": case.marker})
    return job.model_copy(update={"id": case.job_id, "attempt": case.attempt})


def _observe(context: OperationContext) -> RunningKernelObservation:
    recovery = context.activation.recovery_point
    assert recovery is not None
    return context.port.observe(recovery, authority_ref(context))


async def _authority_count(conn: AsyncConnection) -> int:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT count(*) AS n FROM external_boot_authorities")
        row = await cur.fetchone()
    assert row is not None
    return int(row["n"])


async def _job_row(conn: AsyncConnection, job_id: Any) -> dict[str, Any]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT state, attempt, error_category, worker_id FROM jobs WHERE id = %s", (job_id,)
        )
        row = await cur.fetchone()
    assert row is not None
    return dict(row)


def _ports(
    case: SeededCase, *, resolver: ProviderResolver, acknowledger: object | None = None
) -> ExternalBootHandlerPorts:
    return ExternalBootHandlerPorts(
        resolver=resolver,
        incarnation_credential=SecretStr(case.credential),
        acknowledger=cast(Any, acknowledger),
    )


async def _run(
    conn: AsyncConnection,
    case: SeededCase,
    *,
    ports: ExternalBootHandlerPorts,
    marker: ExternalBootAuthorityMarkerV1 | None = None,
    require_activation_state: frozenset[ExternalBootActivationState] = ACTIVATING,
    require_activation_evidence: frozenset[str] = NO_EVIDENCE,
    require_preconditions: Callable[..., Awaitable[None]] = _no_preconditions,
    call_port: Callable[[OperationContext], RunningKernelObservation | None] = _observe,
) -> tuple[OperationContext, RunningKernelObservation | None]:
    """Drive one operation and hand back what ``build_result`` was given.

    ``build_result`` returns a ``None`` the runner only passes through — Task 3 is about the
    admission order and the failure wrapping, and composing real evidence is the operation
    handlers' job. The cast is confined to this helper so no production path sees it.
    """
    captured: list[tuple[OperationContext, RunningKernelObservation | None]] = []

    def build(
        context: OperationContext, observation: RunningKernelObservation | None
    ) -> ExternalBootAuthorityResultV1:
        captured.append((context, observation))
        return cast(ExternalBootAuthorityResultV1, None)

    await run_operation(
        conn,
        _job(case),
        marker or _marker(case),
        ports=ports,
        require_activation_state=require_activation_state,
        require_activation_evidence=require_activation_evidence,
        require_preconditions=require_preconditions,
        call_port=call_port,
        build_result=build,
    )
    return captured[0]


def _drive(migrated_url: str, body: Callable[..., Awaitable[None]], dsn: str | None = None) -> None:
    async def _main() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as seed:
            if dsn is None:
                await body(seed, seed)
                return
            async with await role_connection(dsn) as worker:
                await body(seed, worker)

    asyncio.run(_main())


def test_provider_kind_mismatch_is_refused_before_allocation(
    migrated_url: str, vehicle: Vehicle
) -> None:
    """Charter criterion 3's execution-time half.

    Asserting only the exception would pass even if allocation had already run, which is why the
    authority-row count is asserted too — that is what makes the refusal "rather than at
    ``allocate_external_boot_authority``".
    """

    async def body(seed: AsyncConnection, conn: AsyncConnection) -> None:
        case = await seed_case(seed, vehicle, purpose="activate")
        ports = _ports(case, resolver=resolver_for(vehicle))

        with pytest.raises(CategorizedError, match="provider_kind") as excinfo:
            await _run(
                conn, case, ports=ports, marker=_marker(case, provider_kind="remote-libvirt")
            )

        assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
        assert excinfo.value.terminal is True
        assert await _authority_count(seed) == 0
        assert vehicle.port.calls == []

    _drive(migrated_url, body)


def test_absent_external_boot_port_is_refused(migrated_url: str, vehicle: Vehicle) -> None:
    async def body(seed: AsyncConnection, conn: AsyncConnection) -> None:
        case = await seed_case(seed, vehicle, purpose="activate")
        ports = _ports(case, resolver=provider_resolver())

        with pytest.raises(CategorizedError, match="no external_boot port") as excinfo:
            await _run(conn, case, ports=ports)

        assert excinfo.value.terminal is True
        assert await _authority_count(seed) == 0

    _drive(migrated_url, body)


def test_absent_acknowledger_fails_before_the_port_is_called(
    migrated_url: str, authority_role_dsns: Callable[[str], str], vehicle: Vehicle
) -> None:
    """The seam is absent by default and must fail closed *before* any provider mutation.

    Allocation has already happened here, so the authority row exists — that is the point: the
    refusal lands between allocation and the provider, never after a half-applied mutation.
    """

    async def body(seed: AsyncConnection, conn: AsyncConnection) -> None:
        case = await seed_case(seed, vehicle, purpose="activate")
        ports = _ports(case, resolver=resolver_for(vehicle), acknowledger=None)

        with pytest.raises(CategorizedError, match="acknowledger") as excinfo:
            await _run(conn, case, ports=ports)

        assert excinfo.value.terminal is True
        assert vehicle.port.calls == []
        assert await _authority_count(seed) == 1

    _drive(migrated_url, body, authority_role_dsns("kdive_worker"))


@pytest.mark.parametrize("field", ["run_id", "plan_identity"])
def test_activation_identity_mismatch_is_refused(
    migrated_url: str, vehicle: Vehicle, field: str
) -> None:
    """A marker naming an activation whose identity disagrees is refused before allocation.

    ``system_id`` is not parametrized here because it is refused one step earlier, at
    ``binding_for_system``: an unknown System has no bound runtime at all. That case is
    ``test_unknown_system_is_refused_at_resolution`` below, so the field is covered without
    pretending this check is the one that catches it.
    """
    replacement = {
        "run_id": str(uuid4()),
        "plan_identity": "sha256:" + "9" * 64,
    }[field]

    async def body(seed: AsyncConnection, conn: AsyncConnection) -> None:
        case = await seed_case(seed, vehicle, purpose="activate")
        ports = _ports(case, resolver=resolver_for(vehicle))

        with pytest.raises(CategorizedError, match="does not match the marker") as excinfo:
            await _run(conn, case, ports=ports, marker=_marker(case, **{field: replacement}))

        assert excinfo.value.terminal is True
        assert await _authority_count(seed) == 0

    _drive(migrated_url, body)


def test_unknown_system_is_refused_at_resolution(migrated_url: str, vehicle: Vehicle) -> None:
    async def body(seed: AsyncConnection, conn: AsyncConnection) -> None:
        case = await seed_case(seed, vehicle, purpose="activate")
        ports = _ports(case, resolver=resolver_for(vehicle))

        with pytest.raises(CategorizedError):
            await _run(conn, case, ports=ports, marker=_marker(case, system_id=str(uuid4())))

        assert await _authority_count(seed) == 0

    _drive(migrated_url, body)


def test_activation_state_outside_the_operations_set_is_refused(
    migrated_url: str, vehicle: Vehicle
) -> None:
    """``require_activation_state`` is the **commit** precondition, tighter than ``allocate``'s.

    ``allocate`` admits ``activate`` from ``prepared`` *or* ``activating``; the commit admits only
    ``activating``. Passing ``allocate``'s looser set would let a handler allocate, acknowledge,
    mutate a live System, and only then be refused at commit.
    """

    async def body(seed: AsyncConnection, conn: AsyncConnection) -> None:
        case = await seed_case(seed, vehicle, purpose="activate", activation_state="prepared")
        ports = _ports(case, resolver=resolver_for(vehicle))

        with pytest.raises(CategorizedError, match="does not admit") as excinfo:
            await _run(conn, case, ports=ports)

        assert excinfo.value.terminal is True
        assert await _authority_count(seed) == 0

    _drive(migrated_url, body)


def test_missing_required_evidence_is_refused_before_allocation(
    migrated_url: str, vehicle: Vehicle
) -> None:
    """A NULL evidence column is a categorized refusal, never read as a finished operation.

    ``abandoned`` is admitted by ``external_boot_activation_state_evidence`` on
    ``terminal_evidence`` alone, so this row is a legal one with ``recovery_point`` NULL. Without
    the positive column check the failure would be an uncategorized ``TypeError`` further in — and
    if it landed after allocation, the authority row would already exist.
    """

    async def body(seed: AsyncConnection, conn: AsyncConnection) -> None:
        case = await seed_case(
            seed,
            vehicle,
            purpose="release",
            activation_state="abandoned",
            with_recovery_point=False,
        )
        ports = _ports(case, resolver=resolver_for(vehicle))

        with pytest.raises(CategorizedError, match="has no recovery_point") as excinfo:
            await _run(
                conn,
                case,
                ports=ports,
                require_activation_state=frozenset({ExternalBootActivationState.ABANDONED}),
                require_activation_evidence=frozenset({"recovery_point"}),
            )

        assert excinfo.value.terminal is True
        assert vehicle.port.calls == []
        assert await _authority_count(seed) == 0

    _drive(migrated_url, body)


def test_unmet_precondition_is_refused_before_allocation(
    migrated_url: str, vehicle: Vehicle
) -> None:
    """The per-operation ``require_preconditions`` callable runs as step 2c, before allocation."""

    async def refuse(
        _conn: AsyncConnection,
        _activation: ExternalBootActivation,
        _marker: ExternalBootAuthorityMarkerV1,
    ) -> None:
        raise CategorizedError(
            "the operation's prerequisite is unmet",
            category=ErrorCategory.CONFIGURATION_ERROR,
            terminal=True,
        )

    async def body(seed: AsyncConnection, conn: AsyncConnection) -> None:
        case = await seed_case(seed, vehicle, purpose="activate")
        ports = _ports(case, resolver=resolver_for(vehicle))

        with pytest.raises(CategorizedError, match="prerequisite is unmet"):
            await _run(conn, case, ports=ports, require_preconditions=refuse)

        assert vehicle.port.calls == []
        assert await _authority_count(seed) == 0

    _drive(migrated_url, body)


def test_superseded_allocation_leaves_the_job_row_untouched(
    migrated_url: str, authority_role_dsns: Callable[[str], str], vehicle: Vehicle
) -> None:
    """Records the #2203-owned leak; it does not fix it.

    A ``superseded`` allocation allocates nothing, so there is no binding to commit a failure
    through and the commit's ``fail`` branch — the only path that can requeue a marked job — is
    unreachable. The assertion is that the ``jobs`` row is **unchanged**, not that ``terminal`` is
    ``False``: ``terminal`` has no consumer on this path, so asserting it would assert a value
    nothing reads. Reaping such a job is #2203's; re-entry is #2202's.
    """

    async def body(seed: AsyncConnection, conn: AsyncConnection) -> None:
        # `recovered` is a state allocate refuses for the activate purpose, reached by handing the
        # runner a permissive require_activation_state so the refusal happens at allocate.
        case = await seed_case(seed, vehicle, purpose="activate", activation_state="recovered")
        ports = _ports(case, resolver=resolver_for(vehicle))
        before = await _job_row(seed, case.job_id)

        with pytest.raises(CategorizedError, match="superseded") as excinfo:
            await _run(
                conn,
                case,
                ports=ports,
                require_activation_state=frozenset({ExternalBootActivationState.RECOVERED}),
            )

        assert excinfo.value.category is ErrorCategory.STALE_HANDLE
        assert await _job_row(seed, case.job_id) == before
        assert vehicle.port.calls == []

    _drive(migrated_url, body, authority_role_dsns("kdive_worker"))


def test_a_successful_run_reaches_the_port_and_returns_the_built_result(
    migrated_url: str, authority_role_dsns: Callable[[str], str], vehicle: Vehicle
) -> None:
    """The happy path, so the failure tests below are known to be failing for their own reason."""

    async def body(seed: AsyncConnection, conn: AsyncConnection) -> None:
        case = await seed_case(seed, vehicle, purpose="activate")
        ports = _ports(
            case,
            resolver=resolver_for(vehicle),
            acknowledger=RecordingAcknowledger(authority_role_dsns("kdive_provider_authority")),
        )

        context, observation = await _run(conn, case, ports=ports)

        assert vehicle.port.calls == ["observe"]
        assert vehicle.port.recoveries == [vehicle.recovery_point]
        assert observation == vehicle.materialization.kernel_observation
        assert context.acknowledgement.generation == context.authority.generation
        assert await _authority_count(seed) == 1

    _drive(migrated_url, body, authority_role_dsns("kdive_worker"))


def test_provider_exception_becomes_an_authority_failure_bound_to_the_allocation(
    migrated_url: str, authority_role_dsns: Callable[[str], str], vehicle: Vehicle
) -> None:
    """A provider raise is wrapped bound to the same allocation, and the message is dropped.

    ``_authority_binding_matches`` is the worker's gate before the SQL boundary, so the wrap is
    driven through it rather than through a re-implementation of the same nine comparisons.
    """
    secret = "/var/lib/kdive/secret-path-that-must-not-travel"

    def explode(_context: OperationContext) -> RunningKernelObservation:
        raise OSError(secret)

    async def body(seed: AsyncConnection, conn: AsyncConnection) -> None:
        case = await seed_case(seed, vehicle, purpose="activate")
        ports = _ports(
            case,
            resolver=resolver_for(vehicle),
            acknowledger=RecordingAcknowledger(authority_role_dsns("kdive_provider_authority")),
        )

        with pytest.raises(ExternalBootAuthorityFailure) as excinfo:
            await _run(conn, case, ports=ports, call_port=explode)

        failure = excinfo.value
        assert _authority_binding_matches(_marker(case), failure.result) is True
        result = failure.result.result
        assert isinstance(result, _FailureResult)
        assert result.failure_context.phase == "provider-call"
        assert failure.result.journal_sequence > 0
        # `from None`, so the provider's own exception is not chained onto a renderable traceback.
        assert failure.__cause__ is None
        assert secret not in repr(failure.result.model_dump())

    _drive(migrated_url, body, authority_role_dsns("kdive_worker"))
