"""Real-Postgres proofs for ADR-0614's cleanup-before-credit boundary."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, LiteralString, cast
from uuid import NAMESPACE_URL, uuid5

import psycopg
import pytest
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
                activation_state="recovered",
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

    asyncio.run(run())
