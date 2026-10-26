"""ADR-0805: an authority-observed conflict reaches ``recovery_conflict`` and is resolvable."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

import psycopg
import pytest
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from kdive.domain.operations.jobs import DEFAULT_JOB_DISPATCH_LANE, Job, JobKind
from kdive.jobs import queue
from kdive.jobs.authority_sender import AuthorityRequestSender
from kdive.jobs.external_boot_authority_client import ExternalBootAuthorityClient
from kdive.jobs.handlers.external_boot.ports import ExternalBootHandlerPorts
from kdive.jobs.handlers.external_boot.registrar import build_operations
from kdive.jobs.handlers.external_boot.router import route_marked
from kdive.jobs.models import (
    ExternalBootAuthorityConflictV1,
    ExternalBootAuthorityMarkerV1,
    ExternalBootAuthoritySuccessV1,
    HandlerRegistry,
)
from kdive.jobs.worker import Worker
from kdive.mcp.tools.external_boot.recovery_requests import request_release, resolve_conflict
from kdive.providers.external_boot_authority.journal import FileAuthorityJournal
from kdive.providers.external_boot_authority.protocol import (
    AuthorityCommitContextV1,
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
)
from kdive.providers.external_boot_authority.repository import DatabaseAuthorityRepository
from kdive.providers.external_boot_authority.service import (
    AuthenticatedPeer,
    ExternalBootAuthorityService,
)
from kdive.providers.external_boot_authority.transport import _dispatch
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.jobs.handlers.external_boot.conftest import resolver_for, role_connection
from tests.jobs.handlers.external_boot.seeding import SeededCase, seed_case
from tests.jobs.handlers.external_boot.vehicle import build_vehicle
from tests.mcp.lifecycle import runs_support
from tests.mcp.systems_support import provider_resolver

_THIRD = "sha256:" + "c" * 64
_SOURCE = "sha256:" + "5" * 64


def _ctx() -> RequestContext:
    return RequestContext(
        principal="alice", agent_session="s", projects=("proj",), roles={"proj": Role.ADMIN}
    )


class _ThirdIdentityAdapter:
    """A provider an out-of-band host change moved to a third identity (ADR-0584)."""

    def __init__(
        self,
        *,
        category: Literal["conflict", "mixed"] = "conflict",
        refuses: bool = True,
        restores: bool = True,
    ) -> None:
        self.category = category
        self.refuses = refuses
        self.restores = restores
        self.restored = False
        self.commits: list[str] = []

    async def observe(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1:
        del request
        category, digest = ("source", _SOURCE) if self.restored else (self.category, _THIRD)
        return AuthorityObservationV1(
            observation_id=uuid5(NAMESPACE_URL, digest), category=category, composite_state=digest
        )

    async def commit(
        self, request: AuthorityMutationRequestV1, context: AuthorityCommitContextV1
    ) -> AuthorityObservationV1:
        del context
        self.commits.append(request.operation.value)
        if request.operation.value == "resolve-conflict":
            self.restored = self.restores
        elif self.refuses:
            raise ValueError(
                "external-boot source XML/power state conflicts with recovery metadata"
            )
        return await self.observe(request)


@asynccontextmanager
async def _authority_connection(dsn: str) -> AsyncIterator[AsyncConnection]:
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as connection:
        yield connection


def _service(
    dsns: Callable[[str], str], tmp_path: Path, adapter: _ThirdIdentityAdapter
) -> ExternalBootAuthorityService:
    return ExternalBootAuthorityService(
        repository=DatabaseAuthorityRepository(
            lambda: _authority_connection(dsns("kdive_provider_authority"))
        ),
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=adapter,
    )


def _ports(service: ExternalBootAuthorityService, case: SeededCase) -> ExternalBootHandlerPorts:
    async def authenticate(credential: SecretStr) -> AuthenticatedPeer:
        assert credential.get_secret_value() == case.credential
        return AuthenticatedPeer(case.worker_incarnation)

    class Backend:
        async def _request_frame(self, envelope: bytes, *, deadline: float) -> bytes:
            assert deadline > asyncio.get_running_loop().time()
            return await _dispatch(envelope, authenticate, service)

    sender = AuthorityRequestSender(Backend, lambda: SecretStr(case.credential))
    return ExternalBootHandlerPorts(
        resolver=provider_resolver(external_boot=None),
        incarnation_credential=SecretStr(case.credential),
        secret_registry=SecretRegistry(),
        authority_client_factory=lambda binding, marker, deadline: ExternalBootAuthorityClient(
            sender, marker, deadline
        ),
    )


async def _row(conn: AsyncConnection, sql: Any, args: tuple[object, ...]) -> dict[str, Any]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, args)
        row = await cur.fetchone()
    assert row is not None
    return row


async def _retire_seed_job(conn: AsyncConnection, case: SeededCase) -> None:
    """Free the seeded run for a public request, keeping a resolvable prior authority."""
    await conn.execute("UPDATE jobs SET state = 'succeeded' WHERE id = %s", (case.job_id,))
    await conn.execute(
        "INSERT INTO external_boot_authorities "
        "(system_id, allocation_id, activation_id, run_id, plan_identity, job_id, job_attempt, "
        "purpose, provider_kind, authority_instance, worker_incarnation, operation, "
        "operation_identity, operation_digest, generation, state, acknowledged_at, retired_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,1,'recover','local-libvirt','authority-vehicle',%s,'recover',"
        "%s,%s,1,'retired',now(),now())",
        (
            case.vehicle.system_id,
            case.allocation_id,
            case.vehicle.activation_id,
            case.vehicle.run_id,
            case.vehicle.plan_identity,
            case.job_id,
            case.worker_incarnation,
            "prior-recovery",
            "sha256:" + "2" * 64,
        ),
    )
    await conn.execute(
        "INSERT INTO external_boot_authority_counters (system_id, last_generation) VALUES (%s, 1) "
        "ON CONFLICT (system_id) DO UPDATE SET last_generation = 1",
        (case.vehicle.system_id,),
    )


async def _assert_conflict_recorded(
    seed: AsyncConnection, case: SeededCase, job_id: object, operation: str
) -> None:
    activation = await _row(
        seed,
        "SELECT a.state, a.cleanup_complete, t.state AS attempt_state, t.conflict_evidence "
        "FROM external_boot_activations AS a JOIN external_boot_recovery_attempts AS t "
        "ON t.activation_id = a.id AND t.attempt_id = a.current_attempt_id WHERE a.id = %s",
        (case.vehicle.activation_id,),
    )
    assert (activation["state"], activation["cleanup_complete"], activation["attempt_state"]) == (
        "recovery_conflict",
        False,
        "conflict",
    )
    evidence = activation["conflict_evidence"]
    assert evidence["composite_state"] == _THIRD
    assert evidence["observation_id"] == str(uuid5(NAMESPACE_URL, _THIRD))
    job = await _row(
        seed, "SELECT state, error_category, failure_context FROM jobs WHERE id = %s", (job_id,)
    )
    assert job == {
        "state": "failed",
        "error_category": "conflict",
        "failure_context": {"phase": "observation"},
    }
    audit = await _row(
        seed,
        "SELECT a.state, audit.operation, audit.outcome FROM external_boot_authorities AS a "
        "JOIN external_boot_authority_audit AS audit ON audit.authority_id = a.id "
        "WHERE a.job_id = %s ORDER BY audit.created_at DESC LIMIT 1",
        (job_id,),
    )
    assert audit == {"state": "retired", "operation": operation, "outcome": "result_failed"}


async def _resolve(
    pool: AsyncConnectionPool,
    worker: AsyncConnection,
    case: SeededCase,
    ports: ExternalBootHandlerPorts,
    *,
    restores: bool = True,
) -> object:
    """Admit and run ``systems.resolve_external_boot_conflict`` on the recorded composite.

    With ``restores`` false the provider stays on the third identity, so the resolution re-records
    the conflict through the head-fenced function from its ``recovering`` attempt.
    """
    vehicle = case.vehicle
    admitted = await resolve_conflict(
        pool,
        _ctx(),
        resolver=resolver_for(vehicle),
        system_id=str(vehicle.system_id),
        operation="restore-recorded-source",
        observed_identity=_THIRD,
    )
    assert admitted.status == "queued", admitted.data
    claimed = await queue.dequeue(
        worker, case.worker_incarnation, incarnation_credential=SecretStr(case.credential)
    )
    assert claimed is not None and str(claimed.id) == admitted.object_id
    marker = ExternalBootAuthorityMarkerV1.model_validate(
        claimed.payload["external_boot_authority_v1"]
    )
    handler = build_operations(ports).get("resolve-conflict")
    assert handler is not None
    result = await handler(worker, claimed, marker)
    assert isinstance(result, ExternalBootAuthoritySuccessV1)
    assert result.result.operation == "recovery-attempt"
    assert await queue.complete_external_boot(
        worker, claimed, result, incarnation_credential=SecretStr(case.credential)
    )
    result = await handler(worker, claimed, marker)
    if not restores:
        assert isinstance(result, ExternalBootAuthorityConflictV1)
        recorded = await queue.record_external_boot_conflict(
            worker, claimed, result, incarnation_credential=SecretStr(case.credential)
        )
        assert isinstance(recorded, Job) and recorded.state.value == "failed"
        return claimed.id
    assert isinstance(result, ExternalBootAuthoritySuccessV1)
    completed = await queue.complete_external_boot(
        worker, claimed, result, incarnation_credential=SecretStr(case.credential)
    )
    assert isinstance(completed, Job) and completed.state.value == "succeeded"
    return claimed.id


@pytest.mark.parametrize("restores", [True, False])
def test_release_conflict_is_recorded_through_the_worker_and_resolved(
    migrated_url: str, authority_role_dsns: Callable[[str], str], tmp_path: Path, restores: bool
) -> None:
    async def run() -> None:
        vehicle = build_vehicle()
        adapter = _ThirdIdentityAdapter(restores=restores)
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as seed:
            case = await seed_case(
                seed,
                vehicle,
                purpose="release",
                operation="release",
                activation_state="active",
                with_reservation=True,
            )
            await _retire_seed_job(seed, case)
            ports = _ports(_service(authority_role_dsns, tmp_path, adapter), case)
            async with runs_support.pool(migrated_url) as pool:
                requested = await request_release(
                    pool, _ctx(), run_id=str(vehicle.run_id), resolver=resolver_for(vehicle)
                )
                assert requested.status == "queued", requested.data

                async def must_not_run(_conn: AsyncConnection, _job: Job) -> str:
                    raise AssertionError("a marked release reached the ordinary boot handler")

                registry = HandlerRegistry()
                registry.register(JobKind.BOOT, route_marked(build_operations(ports), must_not_run))
                async with AsyncConnectionPool(
                    authority_role_dsns("kdive_worker"), min_size=5, max_size=5
                ) as worker_pool:
                    worker = Worker(
                        worker_pool,
                        registry,
                        worker_id=case.worker_incarnation,
                        incarnation_credential=SecretStr(case.credential),
                        secret_registry=SecretRegistry(),
                    )
                    claimed = await worker.run_once(DEFAULT_JOB_DISPATCH_LANE)
                assert claimed is not None and str(claimed.id) == requested.object_id
                await _assert_conflict_recorded(seed, case, requested.object_id, "release")
                reservation = await _row(
                    seed,
                    "SELECT count(*) AS n FROM external_boot_reservations WHERE activation_id = %s",
                    (vehicle.activation_id,),
                )
                assert reservation == {"n": 1}

                async with await role_connection(authority_role_dsns("kdive_worker")) as conn:
                    resolve_job = await _resolve(pool, conn, case, ports, restores=restores)
            if not restores:
                await _assert_conflict_recorded(seed, case, resolve_job, "resolve-conflict")
                return
            state = await _row(
                seed,
                "SELECT state FROM external_boot_activations WHERE id = %s",
                (vehicle.activation_id,),
            )
            assert state == {"state": "recovered"}
        assert adapter.commits == ["recover", "resolve-conflict"]

    asyncio.run(run())


async def _run_marked(
    worker: AsyncConnection, case: SeededCase, ports: ExternalBootHandlerPorts
) -> ExternalBootAuthorityConflictV1:
    """Run the seeded job's handler past any intermediate commit to its terminal result."""
    handler = build_operations(ports).get(case.operation)
    assert handler is not None
    job = Job.model_validate(await _row(worker, "SELECT * FROM jobs WHERE id = %s", (case.job_id,)))
    marker = ExternalBootAuthorityMarkerV1.model_validate(case.marker)
    result = await handler(worker, job, marker)
    while isinstance(result, ExternalBootAuthoritySuccessV1):
        assert result.result.operation in {"recovery-attempt", "deadline"}
        assert await queue.complete_external_boot(
            worker, job, result, incarnation_credential=SecretStr(case.credential)
        )
        result = await handler(worker, job, marker)
    assert isinstance(result, ExternalBootAuthorityConflictV1)
    return result


_PURPOSES = [("activate", "activating"), ("recover", "active")]


@pytest.mark.parametrize(("category", "refuses"), [("conflict", True), ("mixed", False)])
@pytest.mark.parametrize(("purpose", "activation_state"), _PURPOSES)
def test_conflict_is_recorded_only_from_the_authority_head(
    migrated_url: str,
    authority_role_dsns: Callable[[str], str],
    tmp_path: Path,
    purpose: str,
    activation_state: str,
    category: Literal["conflict", "mixed"],
    refuses: bool,
) -> None:
    async def run() -> None:
        vehicle = build_vehicle()
        adapter = _ThirdIdentityAdapter(category=category, refuses=refuses)
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as seed:
            case = await seed_case(
                seed, vehicle, purpose=purpose, activation_state=activation_state
            )
            ports = _ports(_service(authority_role_dsns, tmp_path, adapter), case)
            async with await role_connection(authority_role_dsns("kdive_worker")) as worker:
                job = Job.model_validate(
                    await _row(worker, "SELECT * FROM jobs WHERE id = %s", (case.job_id,))
                )
                result = await _run_marked(worker, case, ports)
                evidence = result.result.model_dump(mode="json", by_alias=True)["evidence"]
                forged = [
                    {**evidence, "composite_state": "sha256:" + "d" * 64},
                    {**evidence, "observation_id": str(uuid5(NAMESPACE_URL, "other"))},
                    {**evidence, "objects": [{"ref": "objects/not-this-activation"}]},
                ]
                for document in forged:
                    carrier = ExternalBootAuthorityConflictV1.model_validate(
                        result.model_dump(mode="json", by_alias=True)
                        | {"result": {"operation": "conflict", "evidence": document}}
                    )
                    assert (
                        await queue.record_external_boot_conflict(
                            worker, job, carrier, incarnation_credential=SecretStr(case.credential)
                        )
                        is queue.ExternalBootCommitStatus.SUPERSEDED
                    )
                stale_generation = result.model_copy(update={"generation": result.generation + 1})
                stale_attempt = job.model_copy(update={"attempt": job.attempt + 1})
                for stale_job, stale_result, credential in (
                    (job, stale_generation, case.credential),
                    (stale_attempt, result, case.credential),
                    (job, result, "not-the-worker-credential"),
                ):
                    assert (
                        await queue.record_external_boot_conflict(
                            worker,
                            stale_job,
                            stale_result,
                            incarnation_credential=SecretStr(credential),
                        )
                        is queue.ExternalBootCommitStatus.SUPERSEDED
                    )
                await seed.execute(
                    "UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
                    (case.job_id,),
                )
                assert (
                    await queue.record_external_boot_conflict(
                        worker, job, result, incarnation_credential=SecretStr(case.credential)
                    )
                    is queue.ExternalBootCommitStatus.SUPERSEDED
                )
                untouched = await _row(
                    seed,
                    "SELECT state FROM external_boot_activations WHERE id = %s",
                    (vehicle.activation_id,),
                )
                assert untouched["state"] == (
                    "activating" if purpose == "activate" else "recovering"
                )
                await seed.execute(
                    "UPDATE jobs SET lease_expires_at = now() + interval '5 minutes' WHERE id = %s",
                    (case.job_id,),
                )
                recorded = await queue.record_external_boot_conflict(
                    worker, job, result, incarnation_credential=SecretStr(case.credential)
                )
                assert isinstance(recorded, Job) and recorded.state.value == "failed"
                assert (
                    await queue.record_external_boot_conflict(
                        worker, job, result, incarnation_credential=SecretStr(case.credential)
                    )
                    is queue.ExternalBootCommitStatus.SUPERSEDED
                )
            await _assert_conflict_recorded(seed, case, case.job_id, purpose)
        assert adapter.commits == [purpose]

    asyncio.run(run())


def _head_field(path: str, value: str) -> str:
    return f"head_record = jsonb_set(head_record, '{{{path}}}', '\"{value}\"')"


_HEAD = "UPDATE external_boot_authority_journal_heads SET {} WHERE system_id = %s"
_AUTHORITY = "UPDATE external_boot_authorities SET {} WHERE job_id = %s AND state = 'current'"
_ATTEMPT = "UPDATE external_boot_recovery_attempts SET {} WHERE activation_id = %s"
_JOB = "UPDATE jobs SET {} WHERE id = %s"

# Each arm breaks exactly one binding record_external_boot_conflict_from_head checks (ADR-0805).
_BROKEN_BINDINGS = {
    "head not terminal": (_HEAD, "phase = 'observed'"),
    "head of another operation": (_HEAD, _head_field("operation", "activate-other")),
    "head of another identity": (_HEAD, _head_field("operation_identity", "other-identity")),
    "head of another digest": (_HEAD, _head_field("operation_digest", "sha256:" + "9" * 64)),
    "head of another activation": (
        _HEAD,
        _head_field("activation_id", "00000000-0000-0000-0000-0000000000a1"),
    ),
    "head outcome not conflict": (_HEAD, _head_field("outcome", "target")),
    "head observation source": (_HEAD, _head_field("observation,category", "source")),
    "head observation unreadable": (_HEAD, _head_field("observation,category", "unreadable")),
    "head of another authority": (_HEAD, "authority_id = gen_random_uuid()"),
    "head of another generation": (_HEAD, "generation = generation + 1"),
    "authority retired": (_AUTHORITY, "state = 'retired', retired_at = now()"),
    "attempt of a newer generation": (_ATTEMPT, "authority_generation = authority_generation + 5"),
    "job of another incarnation": (_JOB, "worker_id = 'docker:other'"),
    "authority of another incarnation": (_JOB, "state = state"),
    "authority of another job": (_JOB, "state = state"),
}


@pytest.mark.parametrize("broken", list(_BROKEN_BINDINGS))
def test_conflict_is_refused_when_any_binding_is_broken(
    migrated_url: str,
    authority_role_dsns: Callable[[str], str],
    tmp_path: Path,
    broken: str,
) -> None:
    async def run() -> None:
        vehicle = build_vehicle()
        adapter = _ThirdIdentityAdapter()
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as seed:
            recovering = broken == "attempt of a newer generation"
            case = await seed_case(
                seed,
                vehicle,
                purpose="recover" if recovering else "activate",
                activation_state="active" if recovering else "activating",
            )
            ports = _ports(_service(authority_role_dsns, tmp_path, adapter), case)
            async with await role_connection(authority_role_dsns("kdive_worker")) as worker:
                job = Job.model_validate(
                    await _row(worker, "SELECT * FROM jobs WHERE id = %s", (case.job_id,))
                )
                result = await _run_marked(worker, case, ports)
                if broken == "authority of another job":
                    # A second running job of the same incarnation and attempt.
                    other = await _row(
                        seed,
                        "INSERT INTO jobs (id, kind, payload, state, attempt, max_attempts, "
                        "worker_id, lease_expires_at, heartbeat_at, authorizing, dedup_key) "
                        "SELECT gen_random_uuid(), kind, payload, state, attempt, max_attempts, "
                        "worker_id, lease_expires_at, heartbeat_at, authorizing, 'other-job' "
                        "FROM jobs WHERE id = %s RETURNING id",
                        (case.job_id,),
                    )
                    job = job.model_copy(update={"id": other["id"]})
                template, clause = _BROKEN_BINDINGS[broken]
                credential = case.credential
                await seed.execute(
                    "INSERT INTO worker_incarnations (incarnation, authority_kind, "
                    "authority_binding, credential_hash, fence_protocol) VALUES "
                    "('docker:other', 'docker', '{}'::jsonb, "
                    "sha256(convert_to('another-incarnation-credential', 'UTF8')), 4)"
                )
                if broken == "authority of another incarnation":
                    # The job follows the caller, so only the authority's own binding refuses.
                    credential = "another-incarnation-credential"
                    template, clause = _JOB, "worker_id = 'docker:other'"
                key = {
                    _HEAD: vehicle.system_id,
                    _AUTHORITY: case.job_id,
                    _ATTEMPT: vehicle.activation_id,
                    _JOB: case.job_id,
                }[template]
                await seed.execute(template.format(clause), (key,))  # ty: ignore[invalid-argument-type]
                assert (
                    await queue.record_external_boot_conflict(
                        worker, job, result, incarnation_credential=SecretStr(credential)
                    )
                    is queue.ExternalBootCommitStatus.SUPERSEDED
                )
            untouched = await _row(
                seed,
                "SELECT state FROM external_boot_activations WHERE id = %s",
                (vehicle.activation_id,),
            )
            assert untouched == {"state": "recovering" if recovering else "activating"}

    asyncio.run(run())
