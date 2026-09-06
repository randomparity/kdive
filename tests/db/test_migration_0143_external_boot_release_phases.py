"""Real-Postgres proofs for ADR-0614's cleanup-before-credit boundary."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, LiteralString, cast
from uuid import NAMESPACE_URL, uuid4, uuid5

import psycopg
import pytest
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.providers.external_boot_authority.journal import FileAuthorityJournal
from kdive.providers.external_boot_authority.protocol import (
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
    AuthorityTakeoverRequestV1,
    RecoveryObjectBindingV1,
)
from kdive.providers.external_boot_authority.repository import DatabaseAuthorityRepository
from kdive.providers.external_boot_authority.service import (
    AuthenticatedPeer,
    ExternalBootAuthorityService,
)
from tests.db.external_boot_authority_support import authority_role_dsns as _role_dsns_fixture
from tests.db_waits import wait_until_backend_waiting
from tests.jobs.handlers.external_boot.seeding import (
    AUTHORITY_INSTANCE,
    RESERVED_BYTES,
    owner_key,
    seed_case,
    store_identity,
)
from tests.jobs.handlers.external_boot.vehicle import build_vehicle


@pytest.fixture
def authority_role_dsns(migrated_url: str) -> Any:
    fixture = cast(Any, _role_dsns_fixture).__wrapped__(migrated_url)
    yield next(fixture)
    fixture.close()


async def _scalar(
    conn: psycopg.AsyncConnection, sql: LiteralString, args: tuple[object, ...]
) -> object:
    row = await (await conn.execute(sql, args)).fetchone()
    assert row is not None
    return row[0]


async def _allocate_release_root(
    conn: psycopg.AsyncConnection, case: Any, *, credential: str, attempt: int
) -> dict[str, Any]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM allocate_external_boot_authority("
            "sha256(convert_to(%s,'UTF8')),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                credential,
                case.job_id,
                attempt,
                case.vehicle.activation_id,
                case.vehicle.run_id,
                case.vehicle.system_id,
                case.vehicle.plan_identity,
                "release",
                "local-libvirt",
                AUTHORITY_INSTANCE,
                case.marker["operation_identity"],
            ),
        )
        allocated = await cur.fetchone()
    assert allocated is not None
    assert allocated["status"] == "allocated"
    return allocated


async def _begin_release_recovery(
    conn: psycopg.AsyncConnection,
    *,
    credential: str,
    job_id: object,
    attempt: int,
    authority_id: object,
    generation: object,
    attempt_id: object,
) -> str:
    return str(
        await _scalar(
            conn,
            "SELECT begin_external_boot_derived_release_recovery("
            "sha256(convert_to(%s,'UTF8')),%s,%s,%s,%s,%s,%s)",
            (
                credential,
                job_id,
                attempt,
                authority_id,
                generation,
                attempt_id,
                datetime.now(UTC) + timedelta(minutes=5),
            ),
        )
    )


async def _acknowledge_release_root(
    conn: psycopg.AsyncConnection,
    case: Any,
    authority: dict[str, Any],
    *,
    worker_incarnation: str,
) -> None:
    assert (
        await _scalar(
            conn,
            "SELECT status FROM acknowledge_external_boot_authority("
            "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                authority["authority_id"],
                authority["generation"],
                case.allocation_id,
                case.vehicle.activation_id,
                case.vehicle.run_id,
                case.vehicle.system_id,
                case.vehicle.plan_identity,
                case.job_id,
                authority["generation"],
                "release",
                "local-libvirt",
                AUTHORITY_INSTANCE,
                worker_incarnation,
                "release",
                case.marker["operation_identity"],
                authority["operation_digest"],
                1,
                "sha256:" + "b" * 64,
                "sha256:" + "c" * 64,
            ),
        )
        == "applied"
    )


async def _release_phase(
    conn: psycopg.AsyncConnection,
    case: Any,
    authority: dict[str, Any],
    worker_incarnation: str,
    operation: str,
) -> dict[str, Any]:
    root = {
        "authority_id": str(authority["authority_id"]),
        "generation": authority["generation"],
        "system_id": str(case.vehicle.system_id),
        "activation_id": str(case.vehicle.activation_id),
        "run_id": str(case.vehicle.run_id),
        "plan_identity": case.vehicle.plan_identity,
        "provider_kind": "local-libvirt",
        "authority_instance": AUTHORITY_INSTANCE,
        "worker_incarnation": worker_incarnation,
        "root_operation_identity": case.marker["operation_identity"],
        "root_operation_digest": authority["operation_digest"],
    }
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM derive_external_boot_release_phase_binding(%s::jsonb,%s)",
            (json.dumps(root), operation),
        )
        phase = await cur.fetchone()
    assert phase is not None
    return phase


async def _call_release_function_while_holding_system_lock(
    conn: psycopg.AsyncConnection,
    *,
    function: str,
    case: Any,
    authority: dict[str, Any],
) -> str:
    credential_hash = "sha256(convert_to(%s,'UTF8'))"
    args: tuple[object, ...]
    if function == "begin":
        return await _begin_release_recovery(
            conn,
            credential=case.credential,
            job_id=case.job_id,
            attempt=case.attempt,
            authority_id=authority["authority_id"],
            generation=authority["generation"],
            attempt_id=uuid4(),
        )
    if function == "finalize":
        sql = (
            "SELECT finalize_external_boot_derived_release("
            f"{credential_hash},%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)"
        )
        args = (
            case.credential,
            case.job_id,
            case.attempt,
            authority["authority_id"],
            authority["generation"],
            "sha256:" + "d" * 64,
            json.dumps({}),
            json.dumps({}),
        )
    elif function == "commit":
        sql = (
            "SELECT commit_external_boot_derived_release_recovery("
            f"{credential_hash},%s,%s,%s,%s,%s,%s,%s::jsonb)"
        )
        args = (
            case.credential,
            case.job_id,
            case.attempt,
            authority["authority_id"],
            authority["generation"],
            "sha256:" + "d" * 64,
            "sha256:" + "e" * 64,
            json.dumps({}),
        )
    elif function == "receipt":
        sql = (
            "SELECT record_external_boot_release_cleanup_receipt("
            f"{credential_hash},%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        )
        args = (
            case.credential,
            case.job_id,
            case.attempt,
            authority["authority_id"],
            authority["generation"],
            "sha256:" + "d" * 64,
            "sha256:" + "e" * 64,
            1,
            "sha256:" + "f" * 64,
            "sha256:" + "a" * 64,
        )
    elif function == "adopt":
        sql = (
            "SELECT adopt_external_boot_release_cleanup_receipt_from_head("
            f"{credential_hash},%s,%s,%s,%s,%s)"
        )
        args = (
            case.credential,
            case.job_id,
            case.attempt,
            authority["authority_id"],
            authority["generation"],
            "sha256:" + "a" * 64,
        )
    else:
        raise AssertionError(f"unknown release function {function}")
    return str(await _scalar(conn, sql, args))


class _SourceAuthorityAdapter:
    async def commit(
        self, request: AuthorityMutationRequestV1, context: object
    ) -> AuthorityObservationV1:
        del context
        return await self.observe(request)

    async def observe(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1:
        return AuthorityObservationV1(
            observation_id=request.attempt_id,
            category="source",
            composite_state="sha256:" + "a" * 64,
        )


def _release_takeover(case: Any, authority: dict[str, Any]) -> AuthorityTakeoverRequestV1:
    return AuthorityTakeoverRequestV1(
        authority_id=authority["authority_id"],
        generation=authority["generation"],
        system_id=case.vehicle.system_id,
        activation_id=case.vehicle.activation_id,
        run_id=case.vehicle.run_id,
        plan_identity=case.vehicle.plan_identity,
        purpose="release",
        operation="release",
        provider_kind="local-libvirt",
        authority_instance=AUTHORITY_INSTANCE,
        operation_identity=case.marker["operation_identity"],
        operation_digest=authority["operation_digest"],
    )


@pytest.mark.parametrize("function", ["begin", "finalize", "commit", "receipt", "adopt"])
def test_release_functions_wait_for_system_before_locking_the_authority_row(
    function: str, migrated_url: str, authority_role_dsns: Callable[[str], str]
) -> None:
    """A canonical System→authority transaction never waits on a release phase waiter.

    The phase function deliberately looks up the System ID without a row lock, then takes the
    System advisory lock before locking its authority row.  If the authority-row lock moves above
    that advisory lock, the canonical transaction below would form the inverse wait edge.
    """

    async def run() -> None:
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
        async with await psycopg.AsyncConnection.connect(
            authority_role_dsns("kdive_worker"), autocommit=True
        ) as worker:
            authority = await _allocate_release_root(
                worker, case, credential=case.credential, attempt=case.attempt
            )
        async with await psycopg.AsyncConnection.connect(
            authority_role_dsns("kdive_provider_authority"), autocommit=True
        ) as provider_authority:
            await _acknowledge_release_root(
                provider_authority,
                case,
                authority,
                worker_incarnation=case.worker_incarnation,
            )

        async with (
            await psycopg.AsyncConnection.connect(migrated_url) as canonical,
            await psycopg.AsyncConnection.connect(authority_role_dsns("kdive_worker")) as phase,
        ):
            phase_call: asyncio.Task[str] | None = None
            try:
                async with canonical.transaction():
                    await canonical.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 2125))",
                        (f"kdive:system:{vehicle.system_id}",),
                    )
                    phase_call = asyncio.create_task(
                        _call_release_function_while_holding_system_lock(
                            phase,
                            function=function,
                            case=case,
                            authority=authority,
                        )
                    )
                    await wait_until_backend_waiting(
                        canonical, phase.info.backend_pid, locktype="advisory"
                    )
                    assert not phase_call.done()
                    row = await (
                        await canonical.execute(
                            "SELECT state FROM external_boot_authorities WHERE id = %s "
                            "FOR UPDATE NOWAIT",
                            (authority["authority_id"],),
                        )
                    ).fetchone()
                    assert row == ("current",)
                assert await asyncio.wait_for(phase_call, timeout=5) in {
                    "applied",
                    "superseded",
                    "conflict",
                    "not_applicable",
                }
            finally:
                if phase_call is not None and not phase_call.done():
                    await asyncio.wait_for(phase_call, timeout=5)

    asyncio.run(run())


def test_recovering_release_takeover_refuses_wrong_job_stale_worker_and_bad_terminal_evidence(
    migrated_url: str, authority_role_dsns: Callable[[str], str], tmp_path: Path
) -> None:
    """The narrow recovering-release exception keeps its job, worker, and journal fences."""

    @asynccontextmanager
    async def authority_connection() -> AsyncIterator[AsyncConnection]:
        connection = await psycopg.AsyncConnection.connect(
            authority_role_dsns("kdive_provider_authority"), autocommit=True
        )
        try:
            yield connection
        finally:
            await connection.close()

    service = ExternalBootAuthorityService(
        repository=DatabaseAuthorityRepository(authority_connection),
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=_SourceAuthorityAdapter(),
    )

    async def run() -> None:
        vehicle = build_vehicle()
        retry_incarnation = f"docker:release-retry-{uuid4()}"
        retry_credential = f"release-retry-credential-{uuid4()}"
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as admin:
            case = await seed_case(
                admin,
                vehicle,
                purpose="release",
                operation="release",
                activation_state="active",
                with_reservation=True,
            )
            async with await psycopg.AsyncConnection.connect(
                authority_role_dsns("kdive_worker"), autocommit=True
            ) as original_worker:
                original = await _allocate_release_root(
                    original_worker, case, credential=case.credential, attempt=case.attempt
                )
            await service.acknowledge_takeover(
                AuthenticatedPeer(case.worker_incarnation), _release_takeover(case, original)
            )
            async with await psycopg.AsyncConnection.connect(
                authority_role_dsns("kdive_worker"), autocommit=True
            ) as original_worker:
                assert (
                    await _begin_release_recovery(
                        original_worker,
                        credential=case.credential,
                        job_id=case.job_id,
                        attempt=case.attempt,
                        authority_id=original["authority_id"],
                        generation=original["generation"],
                        attempt_id=uuid4(),
                    )
                    == "applied"
                )
            original_phase = await _release_phase(
                admin, case, original, case.worker_incarnation, "recover"
            )
            await service.execute_mutation(
                AuthenticatedPeer(case.worker_incarnation),
                AuthorityMutationRequestV1(
                    authority_id=original["authority_id"],
                    generation=original["generation"],
                    system_id=vehicle.system_id,
                    activation_id=vehicle.activation_id,
                    run_id=vehicle.run_id,
                    plan_identity=vehicle.plan_identity,
                    purpose="release",
                    operation="recover",
                    provider_kind="local-libvirt",
                    authority_instance=AUTHORITY_INSTANCE,
                    operation_identity=original_phase["operation_identity"],
                    operation_digest=original_phase["operation_digest"],
                    attempt_id=uuid4(),
                    expected_source_identity=vehicle.recovery_point.source_state.definition,
                    intended_target_identity=vehicle.recovery_point.target_state.definition,
                    recovery_objects=(),
                ),
            )
            await admin.execute(
                "INSERT INTO worker_incarnations "
                "(incarnation, authority_kind, authority_binding, credential_hash, fence_protocol) "
                "VALUES (%s, 'docker', '{}'::jsonb, sha256(convert_to(%s, 'UTF8')), 4)",
                (retry_incarnation, retry_credential),
            )
            await admin.execute(
                "UPDATE jobs SET worker_id = %s, attempt = 2, lease_expires_at = now() "
                "+ interval '5 minutes' WHERE id = %s",
                (retry_incarnation, case.job_id),
            )
            async with await psycopg.AsyncConnection.connect(
                authority_role_dsns("kdive_worker"), autocommit=True
            ) as retry_worker:
                replacement = await _allocate_release_root(
                    retry_worker, case, credential=retry_credential, attempt=2
                )
            acknowledgement = await service.acknowledge_takeover(
                AuthenticatedPeer(retry_incarnation), _release_takeover(case, replacement)
            )
            async with await psycopg.AsyncConnection.connect(
                authority_role_dsns("kdive_worker"), autocommit=True
            ) as retry_worker:
                wrong_job = uuid4()
                await admin.execute(
                    "INSERT INTO jobs (id, kind, payload, state, attempt, max_attempts, worker_id, "
                    "lease_expires_at, heartbeat_at, authorizing, dedup_key) "
                    "SELECT %s, kind, payload, 'running', 2, max_attempts, %s, "
                    "now() + interval '5 minutes', now(), authorizing, %s FROM jobs WHERE id = %s",
                    (wrong_job, retry_incarnation, f"external-boot-{wrong_job}", case.job_id),
                )
                assert (
                    await _begin_release_recovery(
                        retry_worker,
                        credential=retry_credential,
                        job_id=wrong_job,
                        attempt=2,
                        authority_id=replacement["authority_id"],
                        generation=replacement["generation"],
                        attempt_id=uuid4(),
                    )
                    == "superseded"
                )
                assert (
                    await _begin_release_recovery(
                        retry_worker,
                        credential=case.credential,
                        job_id=case.job_id,
                        attempt=2,
                        authority_id=replacement["authority_id"],
                        generation=replacement["generation"],
                        attempt_id=uuid4(),
                    )
                    == "superseded"
                )
                attempts = await (
                    await admin.execute(
                        "SELECT authority_generation, state FROM external_boot_recovery_attempts "
                        "WHERE activation_id = %s ORDER BY attempt_number",
                        (vehicle.activation_id,),
                    )
                ).fetchall()
                assert attempts == [(original["generation"], "recovering")]
                assert (
                    await _begin_release_recovery(
                        retry_worker,
                        credential=retry_credential,
                        job_id=case.job_id,
                        attempt=2,
                        authority_id=replacement["authority_id"],
                        generation=replacement["generation"],
                        attempt_id=uuid4(),
                    )
                    == "applied"
                )

            peer = AuthenticatedPeer(retry_incarnation)
            phase = await _release_phase(admin, case, replacement, retry_incarnation, "recover")
            await service.execute_mutation(
                peer,
                AuthorityMutationRequestV1(
                    authority_id=replacement["authority_id"],
                    generation=replacement["generation"],
                    system_id=vehicle.system_id,
                    activation_id=vehicle.activation_id,
                    run_id=vehicle.run_id,
                    plan_identity=vehicle.plan_identity,
                    purpose="release",
                    operation="recover",
                    provider_kind="local-libvirt",
                    authority_instance=AUTHORITY_INSTANCE,
                    operation_identity=phase["operation_identity"],
                    operation_digest=phase["operation_digest"],
                    attempt_id=uuid4(),
                    expected_source_identity=vehicle.recovery_point.source_state.definition,
                    intended_target_identity=vehicle.recovery_point.target_state.definition,
                    recovery_objects=(),
                ),
            )
            assert acknowledgement.authority_id == replacement["authority_id"]
            wrong_terminal = {
                "schema": "external-boot-terminal-evidence-v1",
                "activation_id": str(vehicle.activation_id),
                "system_id": str(vehicle.system_id),
                "outcome": "recovered",
                "composite_state": "sha256:" + "f" * 64,
                "objects": [],
                "observed_at": "2026-09-06T00:00:00Z",
            }
            before_terminal_commit = await (
                await admin.execute(
                    "SELECT state, terminal_evidence FROM external_boot_activations WHERE id = %s",
                    (vehicle.activation_id,),
                )
            ).fetchone()
            async with await psycopg.AsyncConnection.connect(
                authority_role_dsns("kdive_worker"), autocommit=True
            ) as commit_worker:
                assert (
                    await _scalar(
                        commit_worker,
                        "SELECT commit_external_boot_derived_release_recovery("
                        "sha256(convert_to(%s,'UTF8')),%s,%s,%s,%s,%s,%s,%s::jsonb)",
                        (
                            retry_credential,
                            case.job_id,
                            2,
                            replacement["authority_id"],
                            replacement["generation"],
                            phase["operation_identity"],
                            phase["operation_digest"],
                            json.dumps(wrong_terminal),
                        ),
                    )
                    == "superseded"
                )
            activation = await (
                await admin.execute(
                    "SELECT state, terminal_evidence FROM external_boot_activations WHERE id = %s",
                    (vehicle.activation_id,),
                )
            ).fetchone()
            assert activation == before_terminal_commit

    asyncio.run(run())


def test_cleanup_receipt_is_exact_and_final_credit_is_idempotent(
    migrated_url: str, authority_role_dsns: Callable[[str], str], tmp_path: Path
) -> None:
    async def run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as admin:
            vehicle = build_vehicle()
            case = await seed_case(
                admin,
                vehicle,
                purpose="release",
                operation="release",
                activation_state="active",
                with_reservation=True,
                with_release=False,
            )
            async with admin.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT * FROM allocate_external_boot_authority("
                    "sha256(convert_to(%s,'UTF8')),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        case.credential,
                        case.job_id,
                        case.attempt,
                        vehicle.activation_id,
                        vehicle.run_id,
                        vehicle.system_id,
                        vehicle.plan_identity,
                        "release",
                        "local-libvirt",
                        AUTHORITY_INSTANCE,
                        case.marker["operation_identity"],
                    ),
                )
                allocated = await cur.fetchone()
            assert allocated is not None and allocated["status"] == "allocated"
            authority_id, generation = allocated["authority_id"], allocated["generation"]

            @asynccontextmanager
            async def authority_connections() -> Any:
                connection = await psycopg.AsyncConnection.connect(
                    authority_role_dsns("kdive_provider_authority"), autocommit=True
                )
                try:
                    yield connection
                finally:
                    await connection.close()

            class Adapter:
                async def commit(self, request: Any, context: Any) -> AuthorityObservationV1:
                    return await self.observe(request)

                async def observe(self, request: Any) -> AuthorityObservationV1:
                    category = "absent" if request.operation.value == "cleanup" else "source"
                    return AuthorityObservationV1(
                        observation_id=uuid5(NAMESPACE_URL, request.operation_identity),
                        category=category,
                        composite_state="sha256:" + ("a" if category == "absent" else "9") * 64,
                    )

            peer = AuthenticatedPeer(case.worker_incarnation)
            service = ExternalBootAuthorityService(
                repository=DatabaseAuthorityRepository(authority_connections),
                journal_factory=lambda system_id: FileAuthorityJournal(
                    tmp_path, f"{system_id}.journal"
                ),
                adapter=Adapter(),
            )
            takeover = AuthorityTakeoverRequestV1(
                authority_id=authority_id,
                generation=generation,
                system_id=vehicle.system_id,
                activation_id=vehicle.activation_id,
                run_id=vehicle.run_id,
                plan_identity=vehicle.plan_identity,
                purpose="release",
                operation="release",
                provider_kind="local-libvirt",
                authority_instance=AUTHORITY_INSTANCE,
                operation_identity=case.marker["operation_identity"],
                operation_digest=allocated["operation_digest"],
            )
            acknowledgement = await service.acknowledge_takeover(peer, takeover)
            ack_digest = acknowledgement.journal_digest
            root = {
                "authority_id": str(authority_id),
                "generation": generation,
                "system_id": str(vehicle.system_id),
                "activation_id": str(vehicle.activation_id),
                "run_id": str(vehicle.run_id),
                "plan_identity": vehicle.plan_identity,
                "provider_kind": "local-libvirt",
                "authority_instance": AUTHORITY_INSTANCE,
                "worker_incarnation": case.worker_incarnation,
                "root_operation_identity": case.marker["operation_identity"],
                "root_operation_digest": allocated["operation_digest"],
            }
            async with admin.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT * FROM derive_external_boot_release_phase_binding(%s::jsonb,'cleanup')",
                    (json.dumps(root),),
                )
                phase = await cur.fetchone()
            assert phase is not None
            resolved = await (
                await admin.execute(
                    "SELECT operation_identity FROM "
                    "resolve_current_external_boot_release_phase_authority(%s,%s,%s,%s,%s,%s)",
                    (
                        case.worker_incarnation,
                        authority_id,
                        generation,
                        acknowledgement.journal_sequence,
                        ack_digest,
                        "cleanup",
                    ),
                )
            ).fetchone()
            assert resolved == (phase["operation_identity"],)
            stale = await (
                await admin.execute(
                    "SELECT operation_identity FROM "
                    "resolve_current_external_boot_release_phase_authority(%s,%s,%s,%s,%s,%s)",
                    (case.worker_incarnation, authority_id, generation, 99, ack_digest, "cleanup"),
                )
            ).fetchone()
            assert stale is None
            common = dict(
                authority_id=authority_id,
                generation=generation,
                system_id=vehicle.system_id,
                activation_id=vehicle.activation_id,
                run_id=vehicle.run_id,
                plan_identity=vehicle.plan_identity,
                purpose="release",
                provider_kind="local-libvirt",
                authority_instance=AUTHORITY_INSTANCE,
                attempt_id=uuid5(NAMESPACE_URL, case.marker["operation_identity"]),
                expected_source_identity=vehicle.recovery_point.source_state.definition,
                intended_target_identity=vehicle.recovery_point.target_state.definition,
            )
            async with admin.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT * FROM derive_external_boot_release_phase_binding(%s::jsonb,'recover')",
                    (json.dumps(root),),
                )
                recover_phase = await cur.fetchone()
            assert recover_phase is not None
            async with await psycopg.AsyncConnection.connect(
                migrated_url, autocommit=True
            ) as worker:
                assert (
                    await _scalar(
                        worker,
                        "SELECT begin_external_boot_derived_release_recovery("
                        "sha256(convert_to(%s,'UTF8')),%s,%s,%s,%s,%s,%s)",
                        (
                            case.credential,
                            case.job_id,
                            case.attempt,
                            authority_id,
                            generation,
                            uuid5(NAMESPACE_URL, f"{case.marker['operation_identity']}/recover"),
                            datetime.now(UTC) + timedelta(minutes=5),
                        ),
                    )
                    == "applied"
                )
            await service.execute_mutation(
                peer,
                AuthorityMutationRequestV1.model_validate(
                    common
                    | {
                        "operation": "recover",
                        "operation_identity": recover_phase["operation_identity"],
                        "operation_digest": recover_phase["operation_digest"],
                        "recovery_objects": (),
                    }
                ),
            )
            recovered = {
                "schema": "external-boot-terminal-evidence-v1",
                "activation_id": str(vehicle.activation_id),
                "system_id": str(vehicle.system_id),
                "outcome": "recovered",
                "composite_state": acknowledgement.positive_quiescence_digest,
                "objects": [],
                "observed_at": "2026-09-06T00:00:00Z",
            }
            async with await psycopg.AsyncConnection.connect(
                migrated_url, autocommit=True
            ) as worker:
                recovery_args = (
                    case.credential,
                    case.job_id,
                    case.attempt,
                    authority_id,
                    generation,
                    recover_phase["operation_identity"],
                    recover_phase["operation_digest"],
                    json.dumps(recovered),
                )
                recovery_sql = (
                    "SELECT commit_external_boot_derived_release_recovery("
                    "sha256(convert_to(%s,'UTF8')),%s,%s,%s,%s,%s,%s,%s::jsonb)"
                )
                assert await _scalar(worker, recovery_sql, recovery_args) == "applied"
                assert await _scalar(worker, recovery_sql, recovery_args) == "applied"
            cleanup_request = AuthorityMutationRequestV1.model_validate(
                common
                | {
                    "operation": "cleanup",
                    "operation_identity": phase["operation_identity"],
                    "operation_digest": phase["operation_digest"],
                    "recovery_objects": (
                        RecoveryObjectBindingV1(
                            system_id=vehicle.system_id,
                            activation_id=vehicle.activation_id,
                            reference=vehicle.recovery_point.recovery_ref.ref,
                        ),
                    ),
                }
            )
            cleanup_observation = await service.execute_mutation(peer, cleanup_request)
            absent = cleanup_observation.composite_state
            async with admin.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT sequence,digest FROM external_boot_authority_journal_heads "
                    "WHERE system_id=%s AND authority_instance=%s",
                    (vehicle.system_id, AUTHORITY_INSTANCE),
                )
                head = await cur.fetchone()
            assert head is not None
            journal = head["digest"]
            async with await psycopg.AsyncConnection.connect(
                migrated_url, autocommit=True
            ) as worker:
                args = (
                    case.credential,
                    case.job_id,
                    case.attempt,
                    authority_id,
                    generation,
                    phase["operation_identity"],
                    phase["operation_digest"],
                    head["sequence"],
                    journal,
                    absent,
                )
                receipt_sql = (
                    "SELECT record_external_boot_release_cleanup_receipt("
                    "sha256(convert_to(%s,'UTF8')),%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                )
                assert await _scalar(worker, receipt_sql, args) == "applied"
                wrong = (*args[:-1], "sha256:" + "c" * 64)
                assert await _scalar(worker, receipt_sql, wrong) == "superseded"
                release_identity = "sha256:" + "d" * 64
                release = {
                    "schema": "external-boot-release-evidence-v1",
                    "activation_id": str(vehicle.activation_id),
                    "system_id": str(vehicle.system_id),
                    "store_identity": {"ref": store_identity(vehicle)},
                    "owner_key": {"ref": owner_key(vehicle)},
                    "reserved_bytes": RESERVED_BYTES,
                    "enumeration_complete": True,
                    "objects": [],
                    "verified_at": "2026-09-06T00:00:00Z",
                }
                cleanup = {
                    "schema": "external-boot-cleanup-evidence-v1",
                    "activation_id": str(vehicle.activation_id),
                    "system_id": str(vehicle.system_id),
                    "release_identity": release_identity,
                    "mode": "ordinary",
                    "completed_at": "2026-09-06T00:00:01Z",
                }
                final_args = (
                    case.credential,
                    case.job_id,
                    case.attempt,
                    authority_id,
                    generation,
                    release_identity,
                    json.dumps(release),
                    json.dumps(cleanup),
                )
                sql = (
                    "SELECT finalize_external_boot_derived_release("
                    "sha256(convert_to(%s,'UTF8')),%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)"
                )
                assert await _scalar(worker, sql, final_args) == "applied"
                assert await _scalar(worker, sql, final_args) == "applied"
            row = await (
                await admin.execute(
                    "SELECT cleanup_complete,(SELECT count(*) FROM "
                    "external_boot_reservation_releases "
                    "WHERE activation_id=%s) FROM external_boot_activations WHERE id=%s",
                    (vehicle.activation_id, vehicle.activation_id),
                )
            ).fetchone()
            assert row == (True, 1)
            job = await (
                await admin.execute("SELECT state FROM jobs WHERE id=%s", (case.job_id,))
            ).fetchone()
            authority = await (
                await admin.execute(
                    "SELECT state FROM external_boot_authorities WHERE id=%s", (authority_id,)
                )
            ).fetchone()
            assert job == ("succeeded",)
            assert authority == ("retired",)

    asyncio.run(run())


def test_worker_cannot_read_release_receipts_directly(
    authority_role_dsns: Callable[[str], str],
) -> None:
    async def run() -> None:
        async with await psycopg.AsyncConnection.connect(
            authority_role_dsns("kdive_worker"), autocommit=True
        ) as worker:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await worker.execute("SELECT * FROM external_boot_release_cleanup_receipts")

    asyncio.run(run())
