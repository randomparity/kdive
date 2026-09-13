"""Catalog checks for #2349's declared worker write coverage.

The declared inventory boundary intentionally does not consume #2345's broader baseline or
claim to repair its known image-catalog insert leak.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import psycopg

_ROOT = Path(__file__).resolve().parents[2]
_INVENTORY = _ROOT / "tests/jobs/handlers/worker_role_inventory.json"
_WORKER = "kdive_worker"
_DEFINER_SIGNATURE = "public.discharge_system_mutation_obligations(uuid)"


def _coverage() -> list[dict[str, str]]:
    inventory = cast(dict[str, Any], json.loads(_INVENTORY.read_text()))
    return cast(list[dict[str, str]], inventory["worker_write_coverage"])


def _violations(conn: psycopg.Connection[Any], coverage: list[dict[str, str]]) -> list[str]:
    violations: list[str] = []
    for entry in coverage:
        write_id = entry["id"]
        if entry["route"] == "direct":
            row = conn.execute(
                "SELECT has_table_privilege(%s, %s, %s)",
                (_WORKER, entry["table"], entry["privilege"]),
            ).fetchone()
            if row != (True,):
                violations.append(
                    f"{write_id}: missing direct {entry['privilege']} coverage on "
                    f"{entry['table']}; grant the table privilege or declare a lawful "
                    "SECURITY DEFINER function with worker EXECUTE. ADR-0629 prefers the "
                    "fenced function when table access must remain fenced."
                )
            continue

        row = conn.execute(
            """
            SELECT p.prosecdef, has_function_privilege(%s, p.oid, 'EXECUTE')
            FROM pg_proc AS p
            WHERE p.oid = to_regprocedure(%s)
            """,
            (_WORKER, entry["function"]),
        ).fetchone()
        if row is None or row != (True, True):
            violations.append(
                f"{write_id}: {entry['function']} must exist, be SECURITY DEFINER, and grant "
                "EXECUTE to kdive_worker; ADR-0629 deliberately requires no table grant."
            )
    return violations


def test_declared_worker_writes_are_covered_by_migrated_catalog(migrated_url: str) -> None:
    with psycopg.connect(migrated_url) as conn:
        assert _violations(conn, _coverage()) == []


def test_direct_table_grant_check_bites(migrated_url: str) -> None:
    coverage = _coverage()
    with psycopg.connect(migrated_url) as conn:
        conn.execute("REVOKE INSERT ON TABLE public.artifacts FROM kdive_worker")
        violations = _violations(conn, coverage)
        conn.rollback()
    assert any(
        "boot.artifacts.insert: missing direct INSERT coverage" in item for item in violations
    )


def test_definer_execute_check_bites(migrated_url: str) -> None:
    coverage = _coverage()
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "REVOKE EXECUTE ON FUNCTION public.discharge_system_mutation_obligations(uuid) "
            "FROM kdive_worker"
        )
        violations = _violations(conn, coverage)
        conn.rollback()
    assert any(_DEFINER_SIGNATURE in item for item in violations)
