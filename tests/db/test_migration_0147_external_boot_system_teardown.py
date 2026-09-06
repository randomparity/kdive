"""Real-Postgres proofs for authority-owned external-boot System teardown."""

from __future__ import annotations

import psycopg

from kdive.db import migrate


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
