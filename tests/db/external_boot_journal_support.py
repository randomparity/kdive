"""Shared authority-journal and System-teardown helpers for the DB-backed authority tests."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from pydantic import TypeAdapter

from kdive.domain.external_boot_activation import (
    ExternalBootReleaseEvidenceV1,
    ExternalBootTeardownEvidenceV1,
)
from kdive.providers.external_boot_authority.protocol import (
    AuthorityTakeoverRequestV1,
    AuthorityTeardownProofV1,
    JournalPhase,
    JournalRecordV1,
    canonical_record_bytes,
    record_digest,
)
from kdive.providers.external_boot_authority.repository import DatabaseAuthorityRepository

_DIGEST = "sha256:" + "d" * 64


def _record(
    case: Any,
    authority: Any,
    sequence: int,
    previous_digest: str,
    phase: JournalPhase,
    **changes: object,
) -> JournalRecordV1:
    values: dict[str, object] = {
        "authority_id": authority.authority_id,
        "generation": authority.generation,
        "system_id": case.system_id,
        "activation_id": case.activation_id,
        "run_id": case.run_id,
        "plan_identity": "sha256:" + "a" * 64,
        "purpose": case.purpose,
        "operation": case.operation,
        "provider_kind": case.provider_kind,
        "authority_instance": case.authority_instance,
        "operation_identity": case.operation_identity,
        "operation_digest": authority.operation_digest,
        "sequence": sequence,
        "previous_digest": previous_digest,
        "phase": phase,
        "attempt_id": case.job_id,
    }
    if phase not in {
        JournalPhase.WATERMARK_INSTALLED,
        JournalPhase.TAKEOVER_SUPERSEDED,
        JournalPhase.TAKEOVER_ACKNOWLEDGED,
    }:
        values |= {
            "expected_source_identity": "source-a",
            "intended_target_identity": "target-a",
            "recovery_objects": (),
        }
    values.update(changes)
    return JournalRecordV1.model_validate(values)


def _payload(record: JournalRecordV1) -> dict[str, object]:
    return record.model_dump(mode="json", by_alias=True) | {
        "canonical_record": canonical_record_bytes(record).decode()
    }


def _advance_raw(
    conn: psycopg.Connection,
    case: Any,
    authority: Any,
    expected_sequence: int,
    expected_digest: str,
    payload: dict[str, object],
) -> str:
    row = conn.execute(
        "SELECT advance_external_boot_authority_journal_head(%s,%s,%s,%s,%s,%s)",
        (
            case.worker_id,
            authority.authority_id,
            authority.generation,
            expected_sequence,
            expected_digest,
            Jsonb(payload),
        ),
    ).fetchone()
    assert row is not None
    return row[0]


def _promote(
    migrated_url: str, case: Any, authority: Any, acknowledgement: JournalRecordV1
) -> None:
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "UPDATE external_boot_authorities SET state='current', acknowledged_at=now() "
            "WHERE id=%s",
            (authority.authority_id,),
        )
        conn.execute(
            "INSERT INTO external_boot_authority_acknowledgements "
            "(authority_id,system_id,generation,authority_instance,operation_identity,"
            "operation_digest,journal_sequence,journal_digest,positive_quiescence_digest) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                authority.authority_id,
                case.system_id,
                authority.generation,
                case.authority_instance,
                case.operation_identity,
                authority.operation_digest,
                acknowledgement.sequence,
                record_digest(acknowledgement),
                _DIGEST,
            ),
        )


def _database_repository(dsn: str) -> DatabaseAuthorityRepository:
    @asynccontextmanager
    async def connections():
        connection = await psycopg.AsyncConnection.connect(dsn)
        try:
            yield connection
        finally:
            await connection.close()

    return DatabaseAuthorityRepository(connections)


def _takeover_request(case: Any, authority: Any) -> AuthorityTakeoverRequestV1:
    return AuthorityTakeoverRequestV1(
        authority_id=authority.authority_id,
        generation=authority.generation,
        system_id=case.system_id,
        activation_id=case.activation_id,
        run_id=case.run_id,
        plan_identity="sha256:" + "a" * 64,
        purpose=case.purpose,
        operation=case.operation,
        provider_kind=case.provider_kind,
        authority_instance=case.authority_instance,
        operation_identity=case.operation_identity,
        operation_digest=authority.operation_digest,
    )


_PLAN_IDENTITY = "sha256:" + "a" * 64


def _make_ready_prepared(conn: psycopg.Connection, case) -> None:
    """Put the legacy teardown seed in a non-failed public admission state."""
    conn.execute("UPDATE systems SET state = 'ready' WHERE id = %s", (case.system_id,))
    conn.execute("UPDATE runs SET state = 'succeeded' WHERE id = %s", (case.run_id,))
    conn.execute(
        "UPDATE external_boot_activations SET state = 'prepared', current_attempt_id = NULL, "
        "pre_recovery_evidence = NULL, recovery_point = %s, terminal_evidence = NULL, "
        "activation_readiness_deadline = NULL "
        "WHERE id = %s",
        (
            Jsonb(
                {
                    "schema": "external-boot-recovery-v1",
                    "binding": {
                        "system_id": str(case.system_id),
                        "run_id": str(case.run_id),
                        "activation_id": str(case.activation_id),
                    },
                    "plan_identity": _PLAN_IDENTITY,
                }
            ),
            case.activation_id,
        ),
    )


def _proof(case, disposition: str):
    if disposition == "retained_quarantine":
        return TypeAdapter(AuthorityTeardownProofV1).validate_python({"disposition": disposition})
    teardown = {
        "schema": "external-boot-teardown-evidence-v1",
        "system_id": str(case.system_id),
        "system_state": "torn_down",
        "observed_at": "2026-09-06T00:00:00Z",
    }
    teardown_identity = ExternalBootTeardownEvidenceV1.model_validate(teardown).identity
    if disposition == "complete_pending":
        value = {
            "disposition": disposition,
            "teardown_evidence": teardown,
            "cleanup_evidence": {
                "schema": "external-boot-cleanup-evidence-v1",
                "activation_id": str(case.activation_id),
                "system_id": str(case.system_id),
                "mode": "pending_system_teardown",
                "teardown_identity": teardown_identity,
                "completed_at": "2026-09-06T00:00:00Z",
            },
        }
    else:
        release = {
            "schema": "external-boot-release-evidence-v1",
            "activation_id": str(case.activation_id),
            "system_id": str(case.system_id),
            "store_identity": {"ref": "store/private"},
            "owner_key": {"ref": "owner/private"},
            "reserved_bytes": 4096,
            "enumeration_complete": True,
            "objects": [],
            "verified_at": "2026-09-06T00:00:00Z",
        }
        identity = ExternalBootReleaseEvidenceV1.model_validate(release).identity
        value = {
            "disposition": "complete_ready",
            "teardown_evidence": teardown,
            "release_evidence": release,
            "release_identity": identity,
            "cleanup_evidence": {
                "schema": "external-boot-cleanup-evidence-v1",
                "activation_id": str(case.activation_id),
                "system_id": str(case.system_id),
                "release_identity": identity,
                "mode": "system_teardown",
                "teardown_identity": teardown_identity,
                "completed_at": "2026-09-06T00:00:00Z",
            },
        }
    return TypeAdapter(AuthorityTeardownProofV1).validate_python(value)
