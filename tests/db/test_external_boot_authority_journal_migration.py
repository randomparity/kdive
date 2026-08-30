"""Migration 0123 trusted authority journal-head tests (ADR-0584)."""

from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb

from kdive.db import migrate
from kdive.providers.external_boot_authority.protocol import (
    GENESIS_DIGEST,
    JournalPhase,
    JournalRecordV1,
    record_digest,
)
from tests.db.test_external_boot_authority_migration import (
    _allocate,
    _RoleDsns,
    _seed_case,
)
from tests.db.test_external_boot_authority_migration import (
    authority_role_dsns as authority_role_dsns,  # noqa: F401
)

_FUNCTIONS = {
    "resolve_allocating_external_boot_authority(text,uuid,bigint)",
    "resolve_current_external_boot_authority(text,uuid,bigint,bigint,text)",
    "read_external_boot_authority_journal_head(text,uuid,bigint,text)",
    "advance_external_boot_authority_journal_head(text,uuid,bigint,bigint,text,jsonb)",
}


def test_migration_0123_is_the_unique_inventory_tail() -> None:
    migrations = migrate.discover_migrations()
    assert (migrations[-1].version, migrations[-1].filename) == (
        "0123",
        "0123_external_boot_authority_journal.sql",
    )


def test_journal_head_has_bounded_continuations(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    columns = {
        row[0]: row[1]
        for row in pg_conn.execute(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_schema='public' "
            "AND table_name='external_boot_authority_journal_heads'"
        ).fetchall()
    }
    assert columns["pending_takeover"] == "YES"
    assert columns["suspended_operation"] == "YES"
    constraints = {
        row[0]
        for row in pg_conn.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = "
            "'external_boot_authority_journal_heads'::regclass"
        ).fetchall()
    }
    assert {
        "external_boot_journal_sequence_positive",
        "external_boot_journal_generation_positive",
        "external_boot_journal_pending_bounded",
        "external_boot_journal_suspended_bounded",
    } <= constraints


def test_only_authority_role_can_execute_journal_functions(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    for function in _FUNCTIONS:
        for role in (
            "kdive_server",
            "kdive_worker",
            "kdive_reconciler",
            "kdive_lifecycle_witness",
        ):
            assert pg_conn.execute(
                "SELECT has_function_privilege(%s, %s, 'EXECUTE')", (role, function)
            ).fetchone() == (False,)
        assert pg_conn.execute(
            "SELECT has_function_privilege('kdive_provider_authority', %s, 'EXECUTE')",
            (function,),
        ).fetchone() == (True,)


def test_runtime_roles_have_no_direct_journal_table_access(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    for role in (
        "kdive_server",
        "kdive_worker",
        "kdive_reconciler",
        "kdive_lifecycle_witness",
        "kdive_provider_authority",
    ):
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert pg_conn.execute(
                "SELECT has_table_privilege(%s, 'external_boot_authority_journal_heads', %s)",
                (role, privilege),
            ).fetchone() == (False,)


def test_journal_functions_are_security_definer_with_pinned_search_path(
    pg_conn: psycopg.Connection,
) -> None:
    migrate.apply_migrations(pg_conn)
    rows = pg_conn.execute(
        "SELECT p.oid::regprocedure::text, p.prosecdef, p.proconfig "
        "FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid=p.pronamespace "
        "WHERE n.nspname='public' AND p.proname LIKE '%external_boot_authority%journal%'"
    ).fetchall()
    assert {row[0] for row in rows} == {
        "read_external_boot_authority_journal_head(text,uuid,bigint,text)",
        "advance_external_boot_authority_journal_head(text,uuid,bigint,bigint,text,jsonb)",
    }
    assert all(row[1] and row[2] == ['search_path=""'] for row in rows)


def test_allocating_binding_can_create_and_read_exact_genesis_head(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    with psycopg.connect(migrated_url) as conn:
        case = _seed_case(conn, worker_suffix="j")
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)
    with psycopg.connect(
        authority_role_dsns("kdive_provider_authority"), autocommit=True
    ) as provider_authority:
        binding = provider_authority.execute(
            "SELECT * FROM resolve_allocating_external_boot_authority(%s, %s, %s)",
            (case.worker_id, authority.authority_id, authority.generation),
        ).fetchone()
        assert binding is not None
        record = JournalRecordV1.model_validate(
            {
                "authority_id": authority.authority_id,
                "generation": authority.generation,
                "system_id": case.system_id,
                "activation_id": case.activation_id,
                "run_id": case.run_id,
                "plan_identity": "sha256:" + "a" * 64,
                "purpose": case.purpose,
                "provider_kind": case.provider_kind,
                "authority_instance": case.authority_instance,
                "operation_identity": case.operation_identity,
                "operation_digest": authority.operation_digest,
                "sequence": 1,
                "previous_digest": GENESIS_DIGEST,
                "phase": JournalPhase.WATERMARK_INSTALLED,
                "attempt_id": case.job_id,
            }
        )
        payload = record.model_dump(mode="json", by_alias=True) | {
            "record_digest": record_digest(record)
        }
        assert provider_authority.execute(
            "SELECT advance_external_boot_authority_journal_head(%s,%s,%s,%s,%s,%s)",
            (
                case.worker_id,
                authority.authority_id,
                authority.generation,
                0,
                GENESIS_DIGEST,
                Jsonb(payload),
            ),
        ).fetchone() == ("advanced",)
        head = provider_authority.execute(
            "SELECT sequence, digest, phase FROM read_external_boot_authority_journal_head("
            "%s,%s,%s,%s)",
            (
                case.worker_id,
                authority.authority_id,
                authority.generation,
                case.authority_instance,
            ),
        ).fetchone()
        assert head == (1, record_digest(record), "watermark-installed")
