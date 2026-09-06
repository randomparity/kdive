"""Real-Postgres proofs for authority-owned external-boot System teardown."""

from __future__ import annotations

import hashlib
import json

import psycopg
import pytest
from psycopg.types.json import Jsonb
from pydantic import TypeAdapter

from kdive.db import migrate
from kdive.domain.external_boot_activation import (
    ExternalBootReleaseEvidenceV1,
    ExternalBootTeardownEvidenceV1,
)
from kdive.providers.external_boot_authority.protocol import (
    AuthorityTeardownProofV1,
    canonical_teardown_proof_bytes,
    teardown_proof_digest,
)
from tests.db.external_boot_authority_support import (
    _allocate,
    _RoleDsns,
    _seed_case,
    authority_role_dsns,  # noqa: F401
)

_ACK_DIGEST = "sha256:" + "b" * 64
_QUIESCENCE_DIGEST = "sha256:" + "c" * 64


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


def _current(
    conn, case, authority, proof, *, category: str = "absent", composite_state: str | None = None
) -> None:
    conn.execute(
        "UPDATE external_boot_authorities SET state = 'current', acknowledged_at = now() "
        "WHERE id = %s",
        (authority.authority_id,),
    )
    conn.execute(
        "INSERT INTO external_boot_authority_acknowledgements "
        "(authority_id, system_id, generation, authority_instance, operation_identity, "
        "operation_digest, journal_sequence, journal_digest, positive_quiescence_digest) "
        "VALUES (%s,%s,%s,%s,%s,%s,1,%s,%s)",
        (
            authority.authority_id,
            case.system_id,
            authority.generation,
            case.authority_instance,
            case.operation_identity,
            authority.operation_digest,
            _ACK_DIGEST,
            _QUIESCENCE_DIGEST,
        ),
    )
    conn.execute(
        "INSERT INTO external_boot_authority_journal_heads "
        "(authority_instance, system_id, sequence, digest, phase, authority_id, generation, "
        "operation_identity, head_record) VALUES (%s,%s,2,%s,'terminal',%s,%s,%s,%s)",
        (
            case.authority_instance,
            case.system_id,
            _ACK_DIGEST,
            authority.authority_id,
            authority.generation,
            case.operation_identity,
            Jsonb(
                {
                    "observation": {
                        "category": category,
                        "composite_state": composite_state or teardown_proof_digest(proof),
                    }
                }
            ),
        ),
    )


def test_migration_0147_is_registered_after_orphan_authority() -> None:
    versions = [item.version for item in migrate.discover_migrations()]
    assert versions[-1] == "0147"


def test_0147_adds_torn_down_activation_state(migrated_url: str) -> None:
    with psycopg.connect(migrated_url) as conn:
        constraint = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'external_boot_activation_state'"
        ).fetchone()
    assert constraint is not None
    assert "torn_down" in constraint[0]


def test_0147_commits_teardown_as_distinct_activation_terminal_state(migrated_url: str) -> None:
    with psycopg.connect(migrated_url) as conn:
        definition = conn.execute(
            "SELECT pg_get_functiondef("
            "'public.commit_external_boot_authority_result("
            "bytea,uuid,integer,uuid,bigint,uuid,uuid,uuid,text,text,text,text,text,text,"
            "bigint,text,text,jsonb)'::regprocedure)"
        ).fetchone()
    assert definition is not None
    assert "SET state = 'torn_down', cleanup_complete = true" in definition[0]


def test_0147_resolves_only_the_acknowledged_teardown_reservation_snapshot(
    migrated_url: str, request: pytest.FixtureRequest
) -> None:
    role_dsns = request.getfixturevalue("authority_role_dsns")
    assert isinstance(role_dsns, _RoleDsns)
    with psycopg.connect(migrated_url) as seed:
        case = _seed_case(seed, purpose="teardown")
        seed.execute(
            "INSERT INTO external_boot_reservations "
            "(activation_id, store_identity, owner_key, reserved_bytes, state, ready_at) "
            "VALUES (%s, 'store/private', 'owner/private', 4096, 'ready', now())",
            (case.activation_id,),
        )
    with psycopg.connect(role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)
    with psycopg.connect(migrated_url) as seed:
        seed.execute(
            "UPDATE external_boot_authorities SET state = 'current', acknowledged_at = now() "
            "WHERE id = %s",
            (authority.authority_id,),
        )
        seed.execute(
            "INSERT INTO external_boot_authority_acknowledgements "
            "(authority_id, system_id, generation, authority_instance, operation_identity, "
            "operation_digest, journal_sequence, journal_digest, positive_quiescence_digest) "
            "VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s)",
            (
                authority.authority_id,
                case.system_id,
                authority.generation,
                case.authority_instance,
                case.operation_identity,
                authority.operation_digest,
                _ACK_DIGEST,
                _QUIESCENCE_DIGEST,
            ),
        )
    with psycopg.connect(role_dsns("kdive_provider_authority")) as provider:
        row = provider.execute(
            "SELECT * FROM resolve_current_external_boot_teardown_authority(%s,%s,%s,%s,%s)",
            (case.worker_id, authority.authority_id, authority.generation, 1, _ACK_DIGEST),
        ).fetchone()
        assert row is not None
        assert row[-6:] == ("ready", "store/private", "owner/private", 4096, None, None)
        assert (
            provider.execute(
                "SELECT * FROM resolve_current_external_boot_teardown_authority(%s,%s,%s,%s,%s)",
                (case.worker_id, authority.authority_id, authority.generation, 2, _ACK_DIGEST),
            ).fetchone()
            is None
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            provider.execute("SELECT * FROM external_boot_reservations").fetchall()


@pytest.mark.parametrize("disposition", ["complete_ready", "complete_pending"])
def test_0147_finalizes_one_head_bound_teardown_receipt(
    migrated_url: str, request: pytest.FixtureRequest, disposition: str
) -> None:
    role_dsns = request.getfixturevalue("authority_role_dsns")
    assert isinstance(role_dsns, _RoleDsns)
    with psycopg.connect(migrated_url) as seed:
        case = _seed_case(seed, purpose="teardown")
        seed.execute(
            "INSERT INTO external_boot_reservations "
            "(activation_id,store_identity,owner_key,reserved_bytes,state,ready_at) "
            "VALUES (%s,'store/private','owner/private',4096,%s,"
            "CASE WHEN %s = 'ready' THEN now() END)",
            (
                case.activation_id,
                "ready" if disposition == "complete_ready" else "pending",
                "ready" if disposition == "complete_ready" else "pending",
            ),
        )
    with psycopg.connect(role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)
    proof = _proof(case, disposition)
    raw = canonical_teardown_proof_bytes(proof)
    with psycopg.connect(migrated_url) as seed:
        _current(seed, case, authority, proof)
    with psycopg.connect(role_dsns("kdive_worker")) as worker:
        args = (
            case.credential,
            case.job_id,
            case.attempt,
            authority.authority_id,
            authority.generation,
            2,
            _ACK_DIGEST,
            raw,
        )
        assert worker.execute(
            "SELECT finalize_external_boot_authority_teardown(%s,%s,%s,%s,%s,%s,%s,%s)", args
        ).fetchone() == ("applied",)
        assert worker.execute(
            "SELECT finalize_external_boot_authority_teardown(%s,%s,%s,%s,%s,%s,%s,%s)", args
        ).fetchone() == ("applied",)
    with psycopg.connect(migrated_url) as seed:
        state = seed.execute(
            "SELECT state, cleanup_complete FROM external_boot_activations WHERE id=%s",
            (case.activation_id,),
        ).fetchone()
        assert state == ("torn_down", True)
        assert seed.execute(
            "SELECT count(*) FROM external_boot_reservation_releases WHERE activation_id=%s",
            (case.activation_id,),
        ).fetchone() == ((1 if disposition == "complete_ready" else 0),)


def test_0147_rejects_unanchored_or_oversize_teardown_proofs(
    migrated_url: str, request: pytest.FixtureRequest
) -> None:
    role_dsns = request.getfixturevalue("authority_role_dsns")
    assert isinstance(role_dsns, _RoleDsns)
    with psycopg.connect(migrated_url) as seed:
        case = _seed_case(seed, purpose="teardown")
        seed.execute(
            "INSERT INTO external_boot_reservations "
            "(activation_id,store_identity,owner_key,reserved_bytes,state,ready_at) "
            "VALUES (%s,'store/private','owner/private',4096,'ready',now())",
            (case.activation_id,),
        )
    with psycopg.connect(role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)
    proof = _proof(case, "complete_ready")
    raw = canonical_teardown_proof_bytes(proof)
    with psycopg.connect(migrated_url) as seed:
        _current(seed, case, authority, proof)
    with psycopg.connect(role_dsns("kdive_worker")) as worker:
        args = (
            case.credential,
            case.job_id,
            case.attempt,
            authority.authority_id,
            authority.generation,
            2,
            _ACK_DIGEST,
        )
        assert worker.execute(
            "SELECT finalize_external_boot_authority_teardown(%s,%s,%s,%s,%s,%s,%s,%s)",
            args + (b" " + raw,),
        ).fetchone() == ("superseded",)
        with pytest.raises(psycopg.errors.InvalidParameterValue, match="receipt is invalid"):
            worker.execute(
                "SELECT finalize_external_boot_authority_teardown(%s,%s,%s,%s,%s,%s,%s,%s)",
                args + (b"x" * 131073,),
            )


def test_0147_rejects_a_ready_proof_with_a_noncanonical_release_identity(
    migrated_url: str, request: pytest.FixtureRequest
) -> None:
    role_dsns = request.getfixturevalue("authority_role_dsns")
    assert isinstance(role_dsns, _RoleDsns)
    with psycopg.connect(migrated_url) as seed:
        case = _seed_case(seed, purpose="teardown")
        seed.execute(
            "INSERT INTO external_boot_reservations "
            "(activation_id,store_identity,owner_key,reserved_bytes,state,ready_at) "
            "VALUES (%s,'store/private','owner/private',4096,'ready',now())",
            (case.activation_id,),
        )
    with psycopg.connect(role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)
    proof = _proof(case, "complete_ready")
    forged = proof.model_dump(mode="json", by_alias=True)
    forged["release_identity"] = "sha256:" + "f" * 64
    forged["cleanup_evidence"]["release_identity"] = forged["release_identity"]
    raw = json.dumps(forged, sort_keys=True, separators=(",", ":")).encode()
    composite_state = (
        "sha256:" + hashlib.sha256(b"kdive-external-boot-teardown-proof-v1\0" + raw).hexdigest()
    )
    with psycopg.connect(migrated_url) as seed:
        _current(seed, case, authority, proof, composite_state=composite_state)
    with psycopg.connect(role_dsns("kdive_worker")) as worker:
        assert worker.execute(
            "SELECT finalize_external_boot_authority_teardown(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                case.credential,
                case.job_id,
                case.attempt,
                authority.authority_id,
                authority.generation,
                2,
                _ACK_DIGEST,
                raw,
            ),
        ).fetchone() == ("superseded",)


def test_0147_retains_quarantine_without_terminalizing_system(
    migrated_url: str, request: pytest.FixtureRequest
) -> None:
    role_dsns = request.getfixturevalue("authority_role_dsns")
    assert isinstance(role_dsns, _RoleDsns)
    with psycopg.connect(migrated_url) as seed:
        case = _seed_case(seed, purpose="teardown")
        seed.execute(
            "INSERT INTO external_boot_reservations "
            "(activation_id,store_identity,owner_key,reserved_bytes,state,ready_at) "
            "VALUES (%s,'store/private','owner/private',4096,'pending',NULL)",
            (case.activation_id,),
        )
    with psycopg.connect(role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)
    proof = _proof(case, "retained_quarantine")
    with psycopg.connect(migrated_url) as seed:
        _current(seed, case, authority, proof, category="conflict")
    with psycopg.connect(role_dsns("kdive_worker")) as worker:
        assert worker.execute(
            "SELECT finalize_external_boot_authority_teardown(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                case.credential,
                case.job_id,
                case.attempt,
                authority.authority_id,
                authority.generation,
                2,
                _ACK_DIGEST,
                canonical_teardown_proof_bytes(proof),
            ),
        ).fetchone() == ("retained",)
    with psycopg.connect(migrated_url) as seed:
        assert seed.execute(
            "SELECT state FROM systems WHERE id=%s", (case.system_id,)
        ).fetchone() == ("failed",)
        assert seed.execute("SELECT state FROM jobs WHERE id=%s", (case.job_id,)).fetchone() == (
            "queued",
        )
