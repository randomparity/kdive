"""Migration 0132 keeps terminal external-boot discharge inside worker authority SQL."""

from kdive.db import migrate


def _sql() -> str:
    migration = next(item for item in migrate.discover_migrations() if item.version == "0132")
    return migration.sql


def test_0132_is_discovered_at_expected_version() -> None:
    migration = next(item for item in migrate.discover_migrations() if item.version == "0132")
    assert migration.filename == "0132_external_boot_terminal_mutation_discharge.sql"


def test_0132_patches_only_terminal_escape_edges() -> None:
    sql = _sql()
    assert sql.count("mutation_discharge_reason = 'terminal_escape'") == 2
    assert "UPDATE public.systems SET state = 'torn_down'" in sql
    assert "SET state = 'recovery_failed', terminal_evidence = v_evidence" in sql
    assert "recovery_conflict" not in sql
    assert "SET state = 'recovered'" not in sql


def test_0132_preserves_the_worker_owned_security_definer_function() -> None:
    sql = _sql()
    assert "pg_get_functiondef(v_function)" in sql
    assert "EXECUTE v_definition" in sql
    assert "GRANT" not in sql
