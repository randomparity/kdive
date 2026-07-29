"""Migration 0082 indexes `jobs (payload->>'system_id')` (#1561, ADR-0491).

An expression index is only worth anything if the planner actually *matches* it, and it matches
only when the query spells the expression exactly as the index does. So every test here asserts a
query **plan**, not the existence of a `pg_index` row: each one seeds enough rows that a
sequential scan is genuinely the more expensive option, `ANALYZE`s, and reads `EXPLAIN`. The
`_before` test pins the other direction — the same query on the same data one migration earlier
plans as a `Seq Scan` — so the assertions cannot pass vacuously.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import psycopg

from kdive.db import migrate
from kdive.domain.capacity.state import SystemState
from kdive.domain.operations.jobs import JobKind, JobState

_MIGRATION = "0082"
_INDEX = "jobs_payload_system_id_idx"

# Row counts big enough that the planner prefers an index lookup over a seq scan. `jobs` is
# effectively append-only in production, so real tables are far larger; these are the floor, not
# a target.
_SYSTEM_COUNT = 400
_JOBS_PER_SYSTEM = 25


def _apply_before(conn: psycopg.Connection, version: str) -> None:
    for m in migrate.discover_migrations():
        if m.version >= version:
            break
        conn.execute(m.sql.encode())  # bytes: a dynamic str fails ty (see migrate.py:135-138)


def _apply_version(conn: psycopg.Connection, version: str) -> None:
    sql = next(m.sql for m in migrate.discover_migrations() if m.version == version)
    conn.execute(sql.encode())  # bytes: a dynamic str fails ty (see migrate.py:135-138)


def _seed_jobs(conn: psycopg.Connection) -> list[UUID]:
    """Insert `_SYSTEM_COUNT * _JOBS_PER_SYSTEM` system-scoped jobs; return the system ids.

    The states cycle over the whole `JobState` vocabulary and the kinds over the system-lifecycle
    ones, so no single `kind`/`state` predicate is selective on its own — matching production,
    where the System correlation is the selective term and the rest are residual filters.
    """
    system_ids = [uuid4() for _ in range(_SYSTEM_COUNT)]
    kinds = [JobKind.PROVISION.value, JobKind.TEARDOWN.value, JobKind.RESTORE.value]
    states = [state.value for state in JobState]
    conn.execute(
        "INSERT INTO jobs (kind, payload, state, max_attempts, authorizing, dedup_key, "
        "created_at) "
        "SELECT (%(kinds)s::text[])[1 + n %% %(kind_count)s], "
        "       jsonb_build_object('system_id', (%(system_ids)s::text[])[1 + n / %(per_system)s]), "
        "       (%(states)s::text[])[1 + n %% %(state_count)s], 3, '{}'::jsonb, 'dedup-' || n, "
        "       now() - make_interval(secs => n) "
        "FROM generate_series(0, %(total)s - 1) AS n",
        {
            "kinds": kinds,
            "kind_count": len(kinds),
            "states": states,
            "state_count": len(states),
            "system_ids": [str(system_id) for system_id in system_ids],
            "per_system": _JOBS_PER_SYSTEM,
            "total": _SYSTEM_COUNT * _JOBS_PER_SYSTEM,
        },
    )
    conn.execute("ANALYZE jobs")
    return system_ids


def _seed_restoring_system(conn: psycopg.Connection) -> UUID:
    """Insert the resource -> allocation -> System FK chain for one `restoring` System."""
    resource_id, allocation_id, system_id = uuid4(), uuid4(), uuid4()
    conn.execute(
        "INSERT INTO resources (id, kind, pool, cost_class, status, host_uri) "
        "VALUES (%s, 'local-libvirt', 'default', 'standard', 'available', 'qemu:///system')",
        (resource_id,),
    )
    conn.execute(
        "INSERT INTO allocations (id, resource_id, state, principal, project) "
        "VALUES (%s, %s, 'active', 'p', 'proj')",
        (allocation_id, resource_id),
    )
    conn.execute(
        "INSERT INTO systems (id, allocation_id, state, provisioning_profile, principal, project) "
        "VALUES (%s, %s, %s, '{}'::jsonb, 'p', 'proj')",
        (system_id, allocation_id, SystemState.RESTORING.value),
    )
    conn.execute("ANALYZE systems")
    return system_id


def _plan(conn: psycopg.Connection, query: str, params: tuple[object, ...]) -> str:
    rows = conn.execute(b"EXPLAIN " + query.encode(), params).fetchall()
    return "\n".join(str(row[0]) for row in rows)


# Mirrors `latest_succeeded_job_for_system` (src/kdive/jobs/queue.py). The expression must stay
# spelled `payload->>'system_id'` at both ends or the index stops matching.
_LATEST_SUCCEEDED_SQL = (
    "SELECT * FROM jobs WHERE kind = %s AND payload->>'system_id' = %s "
    "AND state = %s ORDER BY created_at DESC, id DESC LIMIT 1"
)

# Mirrors `latest_failed_job_for_system` (src/kdive/jobs/queue.py) — the ADR-0454 read.
_LATEST_FAILED_SQL = (
    "SELECT * FROM jobs WHERE kind = ANY(%s::text[]) AND payload->>'system_id' = %s "
    "AND state = ANY(%s::text[]) ORDER BY created_at DESC, id DESC LIMIT 1"
)

# Mirrors `repair_stalled_restoring_systems` (src/kdive/reconciler/repairs/systems.py) — the
# correlated anti-join, which filters on no constant `system_id` at all.
_STALLED_RESTORING_SQL = (
    "SELECT s.id FROM systems s WHERE s.state = %s AND NOT EXISTS ("
    "  SELECT 1 FROM jobs j WHERE j.kind = %s AND j.payload->>'system_id' = s.id::text "
    "  AND j.state = ANY(%s))"
)

_TERMINAL_STATES = [JobState.SUCCEEDED.value, JobState.FAILED.value, JobState.CANCELED.value]
_ACTIVE_STATES = [JobState.QUEUED.value, JobState.RUNNING.value]


def test_0082_latest_succeeded_lookup_plans_on_the_index(pg_conn: psycopg.Connection) -> None:
    """`runs.get`'s liveness read reaches its System's jobs through the index."""
    migrate.apply_migrations(pg_conn)
    system_id = _seed_jobs(pg_conn)[0]

    plan = _plan(
        pg_conn,
        _LATEST_SUCCEEDED_SQL,
        (JobKind.PROVISION.value, str(system_id), JobState.SUCCEEDED.value),
    )

    assert _INDEX in plan, plan
    assert "Seq Scan on jobs" not in plan, plan


def test_0082_latest_failed_lookup_plans_on_the_index(pg_conn: psycopg.Connection) -> None:
    """ADR-0454's failing-job attribution — `kind = ANY`, `state = ANY` — matches too.

    The `ANY`-list form is the one that could plausibly have differed from the equality form, so
    it gets its own assertion rather than riding on the previous test.
    """
    migrate.apply_migrations(pg_conn)
    system_id = _seed_jobs(pg_conn)[0]

    plan = _plan(
        pg_conn,
        _LATEST_FAILED_SQL,
        ([JobKind.PROVISION.value, JobKind.TEARDOWN.value], str(system_id), _TERMINAL_STATES),
    )

    assert _INDEX in plan, plan
    assert "Seq Scan on jobs" not in plan, plan


def test_0082_stalled_restoring_anti_join_plans_on_the_index(pg_conn: psycopg.Connection) -> None:
    """The reconciler's correlated `NOT EXISTS` uses the index on the inner side.

    This is the shape a `WHERE state = 'failed'` partial index could not have served (ADR-0491
    Decision §3): it compares against `s.id::text` and constrains `state` only to the active set.
    """
    migrate.apply_migrations(pg_conn)
    _seed_jobs(pg_conn)
    _seed_restoring_system(pg_conn)

    plan = _plan(
        pg_conn,
        _STALLED_RESTORING_SQL,
        (SystemState.RESTORING.value, JobKind.RESTORE.value, _ACTIVE_STATES),
    )

    assert _INDEX in plan, plan
    assert "Seq Scan on jobs" not in plan, plan


def test_0082_the_same_lookup_was_a_sequential_scan_before(pg_conn: psycopg.Connection) -> None:
    """One migration earlier, identical data and query: a whole-table `Seq Scan`.

    Without this the three plan assertions above could pass on a planner that never had a choice.
    """
    _apply_before(pg_conn, _MIGRATION)
    system_id = _seed_jobs(pg_conn)[0]

    plan = _plan(
        pg_conn,
        _LATEST_SUCCEEDED_SQL,
        (JobKind.PROVISION.value, str(system_id), JobState.SUCCEEDED.value),
    )

    assert "Seq Scan on jobs" in plan, plan


def test_0082_index_is_unpartitioned_and_single_key(pg_conn: psycopg.Connection) -> None:
    """Pin the shape ADR-0491 chose, not merely that some index exists.

    A partial (`WHERE state = ...`) or `created_at`-trailing variant would still satisfy the plan
    assertions above while taking the write-amplification cost the ADR rejected, so the exact
    definition is asserted.
    """
    _apply_before(pg_conn, _MIGRATION)
    _apply_version(pg_conn, _MIGRATION)

    row = pg_conn.execute(
        "SELECT pg_get_indexdef(indexrelid) FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = %s",
        (_INDEX,),
    ).fetchone()

    assert row is not None, f"{_INDEX} is missing"
    assert str(row[0]) == (
        f"CREATE INDEX {_INDEX} ON public.jobs USING btree (((payload ->> 'system_id'::text)))"
    )


def test_0082_index_references_only_payload_so_state_writes_stay_hot(
    pg_conn: psycopg.Connection,
) -> None:
    """The index references `payload` and nothing else — the HOT property ADR-0491 chose it for.

    A job's `state` is rewritten on every `claim`/`complete`/`fail`, and those stay heap-only-tuple
    updates only while no index references `state`. Read from `pg_depend`, which records a
    column-level dependency for every column an index touches through its keys *or* its predicate
    — the same set PostgreSQL's HOT check consults, so a `WHERE state = 'failed'` variant registers
    `state` here and fails, exactly as it would end HOT eligibility in production.
    """
    _apply_before(pg_conn, _MIGRATION)
    _apply_version(pg_conn, _MIGRATION)

    referenced = {
        str(row[0])
        for row in pg_conn.execute(
            "SELECT a.attname FROM pg_depend d "
            "JOIN pg_class c ON c.oid = d.objid "
            "JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid "
            "WHERE d.classid = 'pg_class'::regclass AND d.refclassid = 'pg_class'::regclass "
            "  AND d.refobjsubid > 0 AND c.relname = %s",
            (_INDEX,),
        ).fetchall()
    }

    assert referenced == {"payload"}
