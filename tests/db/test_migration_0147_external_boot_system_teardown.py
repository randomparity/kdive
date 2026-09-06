"""Real-Postgres proofs for authority-owned external-boot System teardown."""

from __future__ import annotations

import psycopg
import pytest

from kdive.db import migrate
from tests.db.external_boot_authority_support import (
    _allocate,
    _RoleDsns,
    _seed_case,
    authority_role_dsns,  # noqa: F401
)

_ACK_DIGEST = "sha256:" + "b" * 64
_QUIESCENCE_DIGEST = "sha256:" + "c" * 64


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
