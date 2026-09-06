"""The shared runner's admission order, against a real Postgres (spec §6).

Every refusal these tests assert happens **before** allocation, and each asserts that positively —
by counting ``external_boot_authorities`` rows afterwards — rather than by the exception alone. An
exception-only assertion would pass whether or not allocation had already run.

**What the row count proves, stated precisely, because an earlier version of this docstring
over-claimed it.** Refusing early does *not* avoid the ``running`` wedge:
``_finalize_handler`` short-circuits on marker **presence**, not on whether authority exists, so a
pre-allocation refusal writes no ``jobs`` row either and wedges identically. Reaping that is
#2203's. What the count proves is narrower and real — **no authority row, no generation consumed,
no acknowledgement, no provider mutation.** A refusal after allocation has already burned a
generation and may already have mutated a live System. The assertions below are unchanged; only
the claim about what they mean is corrected.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from kdive.db.external_boot_activations import ExternalBootActivationRepository
from kdive.domain.capacity.state import ExternalBootActivationState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.external_boot_activation import ExternalBootActivation
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import queue
from kdive.jobs.handlers.external_boot.ports import ExternalBootHandlerPorts
from kdive.jobs.handlers.external_boot.runner import (
    COMMITTABLE_ERROR_CATEGORIES,
    OperationContext,
    _render_cmdline,
    authority_ref,
    run_operation,
)
from kdive.jobs.models import (
    ExternalBootAuthorityFailure,
    ExternalBootAuthorityMarkerV1,
    ExternalBootAuthorityResultV1,
    _FailureResult,
)
from kdive.jobs.worker import _authority_binding_matches
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.external_boot_authority.journal import FileAuthorityJournal
from kdive.providers.external_boot_authority.protocol import (
    AuthorityObservationV1,
    AuthorityPreparationMutationRequestV1,
    AuthorityPreparationResponseV1,
)
from kdive.providers.external_boot_authority.repository import DatabaseAuthorityRepository
from kdive.providers.external_boot_authority.service import (
    AuthenticatedPeer,
    AuthorityServiceError,
    ExternalBootAuthorityService,
)
from kdive.providers.fault_inject.lifecycle.external_boot import FaultInjectExternalBoot
from kdive.providers.local_libvirt.external_boot_authority import LocalExternalBootAuthorityAdapter
from kdive.providers.ports.external_boot import (
    ExternalBootActivationBinding,
    ExternalBootPreparationObservation,
    OpaqueProviderRef,
    RunningKernelObservation,
)
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.external_boot.routing import AuthorityReservationGeometry
from tests.jobs.handlers.external_boot.conftest import resolver_for, role_connection
from tests.jobs.handlers.external_boot.seeding import (
    RESERVED_BYTES,
    RecordingAcknowledger,
    SeededCase,
    seed_case,
    store_identity,
)
from tests.jobs.handlers.external_boot.support import build_job
from tests.jobs.handlers.external_boot.vehicle import Vehicle, build_vehicle
from tests.mcp.systems_support import provider_resolver
from tests.support.external_boot_plan import external_boot_materialization

ACTIVATING = frozenset({ExternalBootActivationState.ACTIVATING})
NO_EVIDENCE: frozenset[str] = frozenset()


def test_cmdline_rendering_redacts_before_distinct_bounded_escaping() -> None:
    registry = SecretRegistry()
    registry.register("secret\\value", scope=None)
    rendered = _render_cmdline(b"secret\\value \\ literal\x00\x01\xff", Redactor(registry=registry))

    assert "secret" not in rendered
    assert rendered == "[REDACTED] \\\\ literal\\x00\\x01\\xFF"
    assert len(_render_cmdline(b"a" * 9000, Redactor(registry=registry)).encode()) == 8192


def test_preparing_capacity_exhaustion_precedes_materialize_and_retries_after_release(
    migrated_url: str, authority_role_dsns: Callable[[str], str]
) -> None:
    class NoMutation:
        async def execute_preparation(self, request: object) -> object:
            raise AssertionError("capacity exhaustion must precede MATERIALIZE")

    async def body(seed: AsyncConnection, worker: AsyncConnection) -> None:
        target_vehicle = build_vehicle()
        target_store = store_identity(target_vehicle)
        blocker_vehicle = build_vehicle()
        await seed_case(
            seed,
            blocker_vehicle,
            purpose="release",
            activation_state="active",
            with_reservation=True,
            reservation_store_identity=target_store,
        )
        target = await seed_case(
            seed,
            target_vehicle,
            purpose="activate",
            activation_state="preparing",
            with_materialization=False,
            with_recovery_point=False,
        )
        ports = replace(
            _ports(
                target,
                resolver=resolver_for(target_vehicle),
                acknowledger=RecordingAcknowledger(authority_role_dsns("kdive_provider_authority")),
            ),
            preparation_executor=cast(Any, NoMutation()),
            reservation_geometry=lambda _binding: AuthorityReservationGeometry(
                target_store, RESERVED_BYTES, RESERVED_BYTES
            ),
        )
        with pytest.raises(CategorizedError) as raised:
            await _run(
                worker,
                target,
                ports=ports,
                require_activation_state=frozenset({ExternalBootActivationState.PREPARING}),
                call_port=lambda _context: None,
            )
        assert raised.value.category is ErrorCategory.CAPACITY_EXHAUSTED
        row = await (
            await seed.execute(
                "SELECT state FROM external_boot_reservations WHERE activation_id=%s",
                (target.vehicle.activation_id,),
            )
        ).fetchone()
        assert row == ("pending",)
        await seed.execute(
            "DELETE FROM external_boot_reservations WHERE activation_id=%s",
            (blocker_vehicle.activation_id,),
        )
        repository = ExternalBootActivationRepository()
        for _ in range(2):
            status = await repository.mark_reservation_ready_for_job(
                worker,
                credential_hash=hashlib.sha256(target.credential.encode()).digest(),
                job_id=target.job_id,
                job_attempt=target.attempt,
                activation_id=target.vehicle.activation_id,
                store_identity=target_store,
                reserve_bytes=RESERVED_BYTES,
                recovery_max_bytes=RESERVED_BYTES,
            )
            assert status.value == "applied"
        used = await (
            await seed.execute(
                "SELECT sum(reserved_bytes) FROM external_boot_reservations "
                "WHERE store_identity=%s AND state='ready'",
                (target_store,),
            )
        ).fetchone()
        assert used == (RESERVED_BYTES,)

    _drive(migrated_url, body, authority_role_dsns("kdive_worker"))


async def _no_preconditions(
    _conn: AsyncConnection,
    _activation: ExternalBootActivation,
    _marker: ExternalBootAuthorityMarkerV1,
) -> Mapping[str, Any]:
    return {}


def _marker(case: SeededCase, **overrides: Any) -> ExternalBootAuthorityMarkerV1:
    return ExternalBootAuthorityMarkerV1.model_validate(case.marker | overrides)


def _job(case: SeededCase) -> Job:
    kind = JobKind.TEARDOWN if case.purpose == "teardown" else JobKind.BOOT
    key = "system_id" if kind is JobKind.TEARDOWN else "run_id"
    value = case.vehicle.system_id if kind is JobKind.TEARDOWN else case.vehicle.run_id
    job = build_job(
        kind,
        {
            key: str(value),
            "external_boot_authority_v1": case.marker,
            "external_boot_plan_v1": case.vehicle.plan.model_dump(mode="json", by_alias=True),
        },
    )
    return job.model_copy(
        update={
            "id": case.job_id,
            "attempt": case.attempt,
            "worker_id": case.worker_incarnation,
        }
    )


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
        secret_registry=SecretRegistry(),
        acknowledger=cast(Any, acknowledger),
        reservation_geometry=lambda _binding: AuthorityReservationGeometry(
            store_identity(case.vehicle), RESERVED_BYTES, RESERVED_BYTES * 2
        ),
    )


async def _run(
    conn: AsyncConnection,
    case: SeededCase,
    *,
    ports: ExternalBootHandlerPorts,
    marker: ExternalBootAuthorityMarkerV1 | None = None,
    require_activation_state: frozenset[ExternalBootActivationState] = ACTIVATING,
    require_activation_evidence: frozenset[str] = NO_EVIDENCE,
    require_preconditions: Callable[..., Awaitable[Mapping[str, Any]]] = _no_preconditions,
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


def test_preparing_without_executor_refuses_before_authority_or_provider(
    migrated_url: str, authority_role_dsns: Callable[[str], str]
) -> None:
    async def body(seed: AsyncConnection, worker: AsyncConnection) -> None:
        vehicle = build_vehicle()
        case = await seed_case(
            seed,
            vehicle,
            purpose="activate",
            activation_state="preparing",
            with_materialization=False,
            with_recovery_point=False,
        )
        with pytest.raises(CategorizedError, match="preparation executor"):
            await _run(
                worker,
                case,
                ports=_ports(case, resolver=resolver_for(vehicle)),
                require_activation_state=frozenset({ExternalBootActivationState.PREPARING}),
                call_port=lambda _context: (_ for _ in ()).throw(AssertionError("provider called")),
            )
        assert await _authority_count(seed) == 0
        assert vehicle.port.calls == []

    _drive(migrated_url, body, authority_role_dsns("kdive_worker"))


@pytest.mark.parametrize("commit_status", ["applied", "superseded"])
def test_preparing_executes_and_commits_exact_materialization(
    migrated_url: str,
    authority_role_dsns: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
    commit_status: str,
) -> None:
    executed: list[AuthorityPreparationMutationRequestV1] = []
    committed: list[AuthorityPreparationMutationRequestV1] = []
    vehicles: list[Vehicle] = []
    seed_connections: list[AsyncConnection] = []

    class Executor:
        async def execute_preparation(
            self, request: AuthorityPreparationMutationRequestV1
        ) -> AuthorityPreparationResponseV1:
            executed.append(request)
            receipt = ExternalBootPreparationObservation(
                state="materialized",
                binding=ExternalBootActivationBinding(
                    system_id=str(request.system_id),
                    run_id=str(request.run_id),
                    activation_id=str(request.activation_id),
                ),
                plan_identity=request.plan_identity,
                authority=OpaqueProviderRef(
                    ref=f"authority/{request.authority_id}/{request.generation}/{request.attempt_id}"
                ),
                operation_identity=request.operation_identity,
                materialization=external_boot_materialization(request.plan),
            )
            observation = AuthorityObservationV1(
                observation_id=uuid4(), category="target", composite_state=receipt.identity
            )
            return AuthorityPreparationResponseV1(
                observation=observation,
                receipt=receipt,
                journal_sequence=4,
                journal_digest="sha256:" + "d" * 64,
            )

    async def commit(_conn: AsyncConnection, **values: Any) -> str:
        committed.append(values["request"])
        if values["request"].operation.value == "prepare" and commit_status == "applied":
            await seed_connections[0].execute(
                "UPDATE external_boot_activations SET state='prepared', materialization=%s, "
                "recovery_point=%s WHERE id=%s",
                (
                    Jsonb(vehicles[0].materialization_json),
                    Jsonb(vehicles[0].recovery_point_json),
                    vehicles[0].activation_id,
                ),
            )
        return commit_status

    monkeypatch.setattr(
        "kdive.jobs.handlers.external_boot.runner.commit_external_boot_preparation_result",
        commit,
    )

    async def body(seed: AsyncConnection, worker: AsyncConnection) -> None:
        vehicle = build_vehicle()
        vehicles.append(vehicle)
        seed_connections.append(seed)
        case = await seed_case(
            seed,
            vehicle,
            purpose="activate",
            activation_state="preparing",
            with_materialization=False,
            with_recovery_point=False,
        )
        ports = replace(
            _ports(
                case,
                resolver=resolver_for(vehicle),
                acknowledger=RecordingAcknowledger(authority_role_dsns("kdive_provider_authority")),
            ),
            preparation_executor=Executor(),
        )
        call = _run(
            worker,
            case,
            ports=ports,
            require_activation_state=frozenset({ExternalBootActivationState.PREPARING}),
            call_port=lambda _context: None,
        )
        if commit_status == "superseded":
            with pytest.raises(CategorizedError, match="materialize commit was superseded"):
                await call
            assert [request.operation.value for request in executed] == ["materialize"]
            return
        await call
        assert executed == committed
        assert executed[0].plan == vehicle.plan
        assert executed[0].operation.value == "materialize"
        assert executed[0].operation_identity.startswith("sha256:")
        assert [request.operation.value for request in executed] == ["materialize", "prepare"]
        assert executed[0].operation_identity != executed[1].operation_identity
        assert executed[0].attempt_id != executed[1].attempt_id

    _drive(migrated_url, body, authority_role_dsns("kdive_worker"))


@pytest.mark.parametrize("restart_mode", ["same", "successor", "unresolved-successor"])
def test_real_authority_service_and_worker_sql_prepare_both_phases(
    migrated_url: str,
    authority_role_dsns: Callable[[str], str],
    tmp_path: Path,
    restart_mode: str,
) -> None:
    async def body(seed: AsyncConnection, worker: AsyncConnection) -> None:
        vehicle = build_vehicle()
        case = await seed_case(
            seed,
            vehicle,
            purpose="activate",
            activation_state="preparing",
            with_materialization=False,
            with_recovery_point=False,
        )
        await seed.execute(
            "UPDATE jobs SET payload = payload || %s WHERE id=%s",
            (
                Jsonb(
                    {"external_boot_plan_v1": vehicle.plan.model_dump(mode="json", by_alias=True)}
                ),
                case.job_id,
            ),
        )

        @asynccontextmanager
        async def authority_connection() -> Any:
            async with await psycopg.AsyncConnection.connect(
                authority_role_dsns("kdive_provider_authority"), autocommit=True
            ) as connection:
                yield connection

        provider = FaultInjectExternalBoot()
        if restart_mode == "unresolved-successor":
            provider.interrupt_after_receipt("materialize")
        service = ExternalBootAuthorityService(
            repository=DatabaseAuthorityRepository(authority_connection),
            journal_factory=lambda system_id: FileAuthorityJournal(
                tmp_path, f"{system_id}.journal"
            ),
            adapter=LocalExternalBootAuthorityAdapter(cast(Any, provider)),
        )
        peer = AuthenticatedPeer(case.worker_incarnation)
        interrupt_after_terminal = restart_mode != "unresolved-successor"

        class Authority:
            current_peer = peer

            async def acknowledge(self, request: Any) -> Any:
                answer = await service.acknowledge_takeover(self.current_peer, request)
                row = await seed.execute(
                    "SELECT allocation_id, job_id, job_attempt, worker_incarnation "
                    "FROM external_boot_authorities WHERE id=%s",
                    (request.authority_id,),
                )
                authority = await row.fetchone()
                assert authority is not None
                async with authority_connection() as connection:
                    committed = await connection.execute(
                        "SELECT status FROM acknowledge_external_boot_authority("
                        "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            request.authority_id,
                            request.generation,
                            authority[0],
                            request.activation_id,
                            request.run_id,
                            request.system_id,
                            request.plan_identity,
                            authority[1],
                            authority[2],
                            request.purpose,
                            request.provider_kind,
                            request.authority_instance,
                            authority[3],
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

            async def execute_preparation(
                self, request: AuthorityPreparationMutationRequestV1
            ) -> AuthorityPreparationResponseV1:
                nonlocal interrupt_after_terminal
                response = await service.execute_preparation(self.current_peer, request)
                if interrupt_after_terminal:
                    interrupt_after_terminal = False
                    raise RuntimeError("worker stopped before preparation commit")
                return response

        authority = Authority()
        ports = replace(
            _ports(case, resolver=resolver_for(vehicle), acknowledger=authority),
            preparation_executor=authority,
        )
        expected_error: type[Exception] = (
            AuthorityServiceError if restart_mode == "unresolved-successor" else RuntimeError
        )
        expected_message = (
            "provider_conflict"
            if restart_mode == "unresolved-successor"
            else "worker stopped before preparation commit"
        )
        with pytest.raises(expected_error, match=expected_message):
            await _run(
                worker,
                case,
                ports=ports,
                require_activation_state=frozenset({ExternalBootActivationState.PREPARING}),
                call_port=lambda _context: None,
            )
        assert provider.preparation_mutations == {"materialize": 1, "prepare": 0}
        if restart_mode != "same":
            new_incarnation = f"docker:external-boot-{uuid4()}"
            new_credential = f"worker-credential-{uuid4()}"
            await seed.execute(
                "UPDATE worker_incarnations SET state='terminated', terminated_at=now(), "
                "outcome='killed' WHERE incarnation=%s",
                (case.worker_incarnation,),
            )
            await seed.execute(
                "INSERT INTO worker_incarnations "
                "(incarnation, authority_kind, authority_binding, credential_hash, fence_protocol) "
                "VALUES (%s, 'docker', '{}'::jsonb, sha256(convert_to(%s, 'UTF8')), 4)",
                (new_incarnation, new_credential),
            )
            await seed.execute(
                "UPDATE jobs SET attempt=2, worker_id=%s WHERE id=%s",
                (new_incarnation, case.job_id),
            )
            case = replace(
                case,
                attempt=2,
                worker_incarnation=new_incarnation,
                credential=new_credential,
            )
            authority.current_peer = AuthenticatedPeer(new_incarnation)
            ports = replace(ports, incarnation_credential=SecretStr(new_credential))
        await _run(
            worker,
            case,
            ports=ports,
            require_activation_state=frozenset({ExternalBootActivationState.PREPARING}),
            call_port=lambda _context: None,
        )
        row = await seed.execute(
            "SELECT state, materialization IS NOT NULL, recovery_point IS NOT NULL "
            "FROM external_boot_activations WHERE id=%s",
            (vehicle.activation_id,),
        )
        assert await row.fetchone() == ("prepared", True, True)
        assert provider.preparation_mutations == {"materialize": 1, "prepare": 1}

    _drive(migrated_url, body, authority_role_dsns("kdive_worker"))


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
    ) -> Mapping[str, Any]:
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
        assert observation is not None
        assert observation.identity == vehicle.materialization.kernel_observation
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
    provider_identifier = "fault-inject://private-provider.example.internal/system-47"
    raw_message = f"provider {provider_identifier} failed while reading {secret}"

    def explode(_context: OperationContext) -> RunningKernelObservation:
        raise OSError(raw_message)

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
        serialized = json.dumps(failure.result.model_dump(mode="json", by_alias=True))
        assert raw_message not in serialized
        assert secret not in serialized
        assert provider_identifier not in serialized

    _drive(migrated_url, body, authority_role_dsns("kdive_worker"))


def test_committable_categories_match_the_migration_exactly() -> None:
    """Gate the one SQL constant this package mirrors in Python.

    ``_bound_failure`` has to decide before the value reaches SQL, and there is no way to ask the
    database for the list — so the set is duplicated, and duplication without a gate is drift
    waiting to happen. This parses the migration's ``NOT IN`` list and compares, so adding a
    category to the schema without adding it here is a red test rather than a job that wedges with
    no attribution.
    """
    sql = Path("src/kdive/db/schema/0122_external_boot_authority.sql").read_text()
    block = sql.split("p_result ->> 'error_category' NOT IN (", 1)[1].split(")", 1)[0]
    accepted = frozenset(re.findall(r"'([a-z_]+)'", block))

    assert accepted, "the migration's accepted-category list was not found"
    assert {category.value for category in COMMITTABLE_ERROR_CATEGORIES} == accepted
    assert tuple(
        category.value for category in ErrorCategory if category in COMMITTABLE_ERROR_CATEGORIES
    ) == (
        "configuration_error",
        "missing_dependency",
        "build_failure",
        "boot_timeout",
        "readiness_failure",
        "debug_attach_failure",
        "infrastructure_failure",
        "stale_handle",
        "transport_conflict",
        "not_implemented",
        "allocation_denied",
        "lease_expired",
        "provisioning_failure",
        "install_failure",
        "transport_failure",
        "control_failure",
        "authorization_denied",
    )


@pytest.mark.parametrize(
    "category",
    list(ErrorCategory),
)
def test_every_provider_category_maps_to_a_committable_failure(
    migrated_url: str,
    authority_role_dsns: Callable[[str], str],
    vehicle: Vehicle,
    category: ErrorCategory,
) -> None:
    """Enumerate the closed fault vocabulary and prove every result can be committed.

    ``ErrorCategory`` has 24 members and the commit accepts 17. Copying an unaccepted one through
    raises SQLSTATE ``22023`` from inside the commit — and that call sits **outside**
    ``_finalize_handler``'s ``try/except``, so it escapes to ``_claim_loop`` and surfaces as
    ``run_once failed on lane %s`` with no job id. Nothing else catches it:
    ``_FailureResult.error_category`` is typed as the whole enum so pydantic passes it, and
    ``_authority_binding_matches`` never looks at it.

    Not constructible through a composed adapter today, which is exactly why it is worth pinning:
    ``providers/remote_libvirt/lifecycle/external_boot.py`` already raises ``CONFLICT`` at ``:496``
    and ``:928`` and ``NOT_FOUND`` at ``:536``, and that is the module #2199/#2200 compose. The
    assertion is that the commit **applies**, not merely that the category changed — the point is a
    committable result, not a tidier field.
    """

    def explode(_context: OperationContext) -> RunningKernelObservation:
        raise CategorizedError("provider says no", category=category, terminal=True)

    async def body(seed: AsyncConnection, conn: AsyncConnection) -> None:
        case = await seed_case(seed, vehicle, purpose="activate")
        ports = _ports(
            case,
            resolver=resolver_for(vehicle),
            acknowledger=RecordingAcknowledger(authority_role_dsns("kdive_provider_authority")),
        )

        with pytest.raises(ExternalBootAuthorityFailure) as excinfo:
            await _run(conn, case, ports=ports, call_port=explode)

        result = excinfo.value.result.result
        assert isinstance(result, _FailureResult)
        expected = (
            category
            if category in COMMITTABLE_ERROR_CATEGORIES
            else ErrorCategory.INFRASTRUCTURE_FAILURE
        )
        assert result.error_category is expected
        committed = await queue.fail_external_boot(
            conn,
            _job(case),
            excinfo.value.result,
            incarnation_credential=SecretStr(case.credential),
        )
        assert committed is not None
        assert (await _job_row(seed, case.job_id))["state"] == "failed"

    _drive(migrated_url, body, authority_role_dsns("kdive_worker"))
