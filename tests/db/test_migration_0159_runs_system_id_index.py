"""Real planner proofs for the two systems.get lookups (#2717, ADR-0701)."""

from __future__ import annotations

import psycopg
import pytest

from kdive.db import migrate
from kdive.mcp.tools.lifecycle.systems.view import RUN_INVESTIGATIONS_LIMIT

_INDEX = "runs_system_id_idx"
# Mirrors _active_run_for_system and _run_history_for_system in systems/view.py.
_ACTIVE = (
    "SELECT id, state FROM runs WHERE system_id = %s AND state <> ALL(%s) "
    "ORDER BY created_at DESC, id LIMIT 1"
)
_HISTORY = (
    "SELECT investigation_id FROM runs WHERE system_id = %s AND project = %s "
    "GROUP BY investigation_id ORDER BY max(created_at) DESC, investigation_id LIMIT %s"
)
# 400 Systems with 100 Runs each, plus 5,000 older Runs on one reused System.
# The real FK chain and mixed states keep the predicate and projection realistic.
_SEED = """
INSERT INTO resources (id,kind,pool,cost_class,status,host_uri)
VALUES (md5('resource')::uuid,'local-libvirt','default','standard','available','qemu:///system');
INSERT INTO allocations (id,resource_id,state,principal,project)
VALUES (md5('allocation')::uuid,md5('resource')::uuid,'active','principal','project');
INSERT INTO systems (id,allocation_id,state,provisioning_profile,principal,project)
SELECT md5('system'||n)::uuid,md5('allocation')::uuid,'ready','{}','principal','project'
FROM generate_series(0,399) n;
INSERT INTO investigations(id,title,state,principal,project)
SELECT md5('investigation'||n)::uuid,'index proof','active','principal','project'
FROM generate_series(0,99) n;
INSERT INTO runs(investigation_id,system_id,state,build_profile,principal,project,
                 target_kind,created_at)
SELECT md5('investigation'||(n%100))::uuid,md5('system'||(n/100))::uuid,
CASE WHEN n%3=0 THEN 'failed' WHEN n%3=1 THEN 'canceled' ELSE 'succeeded' END,
'{}','principal','project','local-libvirt',now()-make_interval(secs=>n)
FROM generate_series(0,39999)n;
INSERT INTO runs(investigation_id,system_id,state,build_profile,principal,project,
                 target_kind,created_at)
SELECT md5('investigation'||(n%100))::uuid,md5('system0')::uuid,
CASE WHEN n%3=0 THEN 'failed' WHEN n%3=1 THEN 'canceled' ELSE 'succeeded' END,
'{}','principal','project','local-libvirt',now()-make_interval(secs=>n)
FROM generate_series(40000,44999)n;
ANALYZE runs;
"""


def _before(conn: psycopg.Connection) -> None:
    for migration in migrate.discover_migrations():
        if migration.version >= "0159":
            break
        conn.execute(migration.sql.encode())


@pytest.mark.parametrize("system", ["system1", "system0"], ids=["ordinary", "reused"])
@pytest.mark.parametrize("history", [False, True], ids=["active", "history"])
def test_0159_both_existing_lookups_gain_an_index(
    pg_conn: psycopg.Connection, system: str, history: bool
) -> None:
    _before(pg_conn)
    pg_conn.execute(_SEED.encode())
    row = pg_conn.execute("SELECT md5(%s)::uuid", (system,)).fetchone()
    assert row is not None
    params = (
        (row[0], "project", RUN_INVESTIGATIONS_LIMIT + 1)
        if history
        else (row[0], ["failed", "canceled"])
    )
    query = _HISTORY if history else _ACTIVE
    before_rows = pg_conn.execute(query.encode(), params).fetchall()
    before_plan = "\n".join(
        str(row[0]) for row in pg_conn.execute(b"EXPLAIN " + query.encode(), params)
    )
    assert "Seq Scan on runs" in before_plan, before_plan

    migration = next(m for m in migrate.discover_migrations() if m.version == "0159")
    # The production runner applies each migration inside a transaction.
    with pg_conn.transaction():
        pg_conn.execute(migration.sql.encode())

    after_plan = "\n".join(
        str(row[0]) for row in pg_conn.execute(b"EXPLAIN " + query.encode(), params)
    )
    assert _INDEX in after_plan, after_plan
    assert "Seq Scan on runs" not in after_plan, after_plan
    assert pg_conn.execute(query.encode(), params).fetchall() == before_rows
    assert len(before_rows) == (RUN_INVESTIGATIONS_LIMIT + 1 if history else 1)


def test_0159_fresh_install_and_rerun_keep_one_unpartitioned_key(
    pg_conn: psycopg.Connection,
) -> None:
    migrate.apply_migrations(pg_conn)
    assert migrate.apply_migrations(pg_conn) == []
    row = pg_conn.execute(
        "SELECT pg_get_indexdef(indexrelid), indisvalid, indisready "
        "FROM pg_index JOIN pg_class ON oid = indexrelid WHERE relname = %s",
        (_INDEX,),
    ).fetchone()
    assert row == (
        "CREATE INDEX runs_system_id_idx ON public.runs USING btree (system_id)",
        True,
        True,
    )
