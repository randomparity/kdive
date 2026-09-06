"""Real-Postgres proofs for ADR-0614's cleanup-before-credit boundary."""

from __future__ import annotations

import asyncio
import json
from typing import LiteralString

import psycopg
from psycopg.rows import dict_row

from tests.jobs.handlers.external_boot.seeding import (
    AUTHORITY_INSTANCE,
    RESERVED_BYTES,
    owner_key,
    seed_case,
    store_identity,
)
from tests.jobs.handlers.external_boot.vehicle import build_vehicle


async def _scalar(
    conn: psycopg.AsyncConnection, sql: LiteralString, args: tuple[object, ...]
) -> object:
    row = await (await conn.execute(sql, args)).fetchone()
    assert row is not None
    return row[0]


def test_cleanup_receipt_is_exact_and_final_credit_is_idempotent(
    migrated_url: str,
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
            await admin.execute(
                "UPDATE external_boot_authorities SET state='current', acknowledged_at=now() "
                "WHERE id=%s",
                (authority_id,),
            )
            ack_digest = "sha256:" + "1" * 64
            await admin.execute(
                "INSERT INTO external_boot_authority_acknowledgements "
                "(authority_id,system_id,generation,authority_instance,operation_identity,"
                "operation_digest,journal_sequence,journal_digest,positive_quiescence_digest) "
                "VALUES (%s,%s,%s,%s,%s,%s,1,%s,%s)",
                (
                    authority_id,
                    vehicle.system_id,
                    generation,
                    AUTHORITY_INSTANCE,
                    case.marker["operation_identity"],
                    allocated["operation_digest"],
                    ack_digest,
                    "sha256:" + "2" * 64,
                ),
            )
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
                    "resolve_current_external_boot_release_phase_authority(%s,%s,%s,1,%s,%s)",
                    (case.worker_incarnation, authority_id, generation, ack_digest, "cleanup"),
                )
            ).fetchone()
            assert resolved == (phase["operation_identity"],)
            stale = await (
                await admin.execute(
                    "SELECT operation_identity FROM "
                    "resolve_current_external_boot_release_phase_authority(%s,%s,%s,2,%s,%s)",
                    (case.worker_incarnation, authority_id, generation, ack_digest, "cleanup"),
                )
            ).fetchone()
            assert stale is None
            absent = "sha256:" + "a" * 64
            journal = "sha256:" + "b" * 64
            await admin.execute(
                "INSERT INTO external_boot_authority_journal_heads "
                "(system_id,authority_instance,authority_id,generation,sequence,digest,phase,"
                "operation_identity,head_record) "
                "VALUES (%s,%s,%s,%s,1,%s,'terminal',%s,%s::jsonb)",
                (
                    vehicle.system_id,
                    AUTHORITY_INSTANCE,
                    authority_id,
                    generation,
                    journal,
                    phase["operation_identity"],
                    json.dumps(
                        {
                            "operation": "cleanup",
                            "operation_identity": phase["operation_identity"],
                            "operation_digest": phase["operation_digest"],
                            "observation": {"category": "absent", "composite_state": absent},
                        }
                    ),
                ),
            )
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
                    1,
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
