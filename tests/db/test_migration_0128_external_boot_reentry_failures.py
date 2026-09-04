"""Migration 0128 closes external-boot re-entry failures and replay (#2202)."""

import psycopg

from kdive.db import migrate


def _sql() -> str:
    migration = next(item for item in migrate.discover_migrations() if item.version == "0128")
    return migration.sql


def test_0128_is_latest_migration() -> None:
    assert (
        migrate.discover_migrations()[-1].version,
        migrate.discover_migrations()[-1].filename,
    ) == ("0128", "0128_external_boot_reentry_failures.sql")


def test_0128_preserves_commit_signature_and_worker_privilege() -> None:
    sql = _sql()
    assert "CREATE OR REPLACE FUNCTION public.commit_external_boot_authority_result(" in sql
    assert "SECURITY DEFINER" in sql
    assert "SET search_path = ''" in sql
    assert "GRANT EXECUTE ON FUNCTION public.commit_external_boot_authority_result(" in sql
    assert "text, bigint, text, text, jsonb) TO kdive_worker" in sql


def test_0128_applies_without_changing_function_security(migrated_url: str) -> None:
    signature = (
        "public.commit_external_boot_authority_result(bytea,uuid,integer,uuid,bigint,uuid,"
        "uuid,uuid,text,text,text,text,text,text,bigint,text,text,jsonb)"
    )
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT prosecdef, proconfig FROM pg_proc WHERE oid = %s::regprocedure",
            (signature,),
        ).fetchone() == (True, ['search_path=""'])
        assert conn.execute(
            "SELECT has_function_privilege('kdive_worker', %s, 'EXECUTE')", (signature,)
        ).fetchone() == (True,)


def test_0128_validates_exact_cas_failure_contract() -> None:
    sql = _sql()
    for reason, action, terminal in (
        ("observed_identity_stale", "systems.get", "true"),
        ("reservation_not_ready", "jobs.wait", "false"),
        ("authority_superseded", "jobs.get", "true"),
    ):
        assert reason in sql
        assert action in sql
        assert terminal in sql
    assert "field NOT IN ('phase', 'reason', 'next_action')" in sql


def test_0128_has_equal_replay_and_conflicting_replay_paths() -> None:
    sql = _sql()
    assert "activation_readiness_deadline = v_deadline" in sql
    assert "activation_readiness_deadline <> v_deadline" in sql
    assert "recovery_readiness_deadline = v_deadline" in sql
    assert "recovery_readiness_deadline <> v_deadline" in sql
    assert "attempt_id = (p_result ->> 'attempt_id')::uuid" in sql
    assert "recovery_basis = p_result ->> 'recovery_basis'" in sql
    assert "RETURN QUERY SELECT 'superseded'::text, NULL::text" in sql


def test_0128_classifies_losing_commits_from_locked_rows() -> None:
    sql = _sql()
    for status in ("observed_identity_stale", "authority_superseded"):
        assert f"RETURN QUERY SELECT '{status}'::text, 'failed'::text" in sql
    assert "RETURN QUERY SELECT 'reservation_not_ready'::text, NULL::text" in sql
    assert "v_activation.system_id <> p_system_id" in sql
    assert "reservation.state = 'ready'" in sql
