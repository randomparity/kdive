"""Reviewed supersessions of applied migrations (ADR-0818, #3612)."""

from __future__ import annotations

import hashlib

import psycopg
import pytest

from kdive.db import migrate, supersessions
from kdive.db.supersessions import Supersession

_A = "a" * 64
_B = "b" * 64
_C = "c" * 64


def _entry(version: str = "0112", old: str = _A, new: str = _B) -> Supersession:
    return Supersession(version, f"{version}_x.sql", old, new, "ADR-0818")


def test_admits_the_exact_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supersessions, "SUPERSESSIONS", (_entry(),))
    assert supersessions.admits("0112", _A, _B)


@pytest.mark.parametrize(
    ("version", "recorded", "current"),
    [("0113", _A, _B), ("0112", _C, _B), ("0112", _A, _C), ("0112", _B, _A)],
)
def test_admits_nothing_else(
    monkeypatch: pytest.MonkeyPatch, version: str, recorded: str, current: str
) -> None:
    monkeypatch.setattr(supersessions, "SUPERSESSIONS", (_entry(),))
    assert not supersessions.admits(version, recorded, current)


def _recorded_0112(conn: psycopg.Connection) -> str:
    row = conn.execute("SELECT checksum FROM schema_migrations WHERE version = '0112'").fetchone()
    assert row is not None
    return row[0]


def test_runner_accepts_an_exact_supersession(
    pg_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrate.apply_migrations(pg_conn)
    current = _recorded_0112(pg_conn)
    pg_conn.execute("UPDATE schema_migrations SET checksum = %s WHERE version = '0112'", (_A,))
    monkeypatch.setattr(supersessions, "SUPERSESSIONS", (_entry(old=_A, new=current),))

    assert migrate.apply_migrations(pg_conn) == []
    assert _recorded_0112(pg_conn) == _A  # history is never rewritten


def test_runner_rejects_a_supersession_whose_current_bytes_differ(
    pg_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrate.apply_migrations(pg_conn)
    pg_conn.execute("UPDATE schema_migrations SET checksum = %s WHERE version = '0112'", (_A,))
    monkeypatch.setattr(supersessions, "SUPERSESSIONS", (_entry(old=_A, new=_C),))

    with pytest.raises(migrate.MigrationError, match="checksum changed"):
        migrate.apply_migrations(pg_conn)


_HAZARD_SQL = """
SELECT p.oid::regprocedure::text
FROM pg_catalog.pg_proc AS p
JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND p.prosqlbody IS NOT NULL
  AND EXISTS (
      SELECT 1
      FROM unnest(coalesce(p.proallargtypes, p.proargtypes::oid[]) || p.prorettype) AS t(oid)
      JOIN pg_catalog.pg_type AS ty ON ty.oid = t.oid
      LEFT JOIN pg_catalog.pg_type AS el ON el.oid = ty.typelem AND ty.typcategory = 'A'
      JOIN pg_catalog.pg_class AS c ON c.oid = coalesce(el.typrelid, ty.typrelid)
      WHERE c.relkind IN ('r', 'p')
  )
ORDER BY 1
"""

# The original 0112 definitions (new-style bodies) that every pre-#3612 database still holds.
_ORIGINAL_NEW_STYLE = """
CREATE OR REPLACE FUNCTION public.capture_launch_abort_evidence_valid(
    p_operation public.capture_operations,
    p_evidence jsonb
) RETURNS boolean
LANGUAGE sql
IMMUTABLE
SET search_path = ''
RETURN jsonb_typeof(p_evidence) = 'object'
   AND (p_evidence ->> 'evidence_kind') = 'closed_gate_boundary_token_scan_v1'
   AND (p_evidence -> 'gate_closed') = 'true'::jsonb
   AND (p_evidence -> 'boundary_scan_complete') = 'true'::jsonb
   AND (p_evidence -> 'boundary_processes_absent') = 'true'::jsonb
   AND jsonb_typeof(p_evidence -> 'host_instance') = 'string'
   AND (p_evidence ->> 'host_instance') = p_operation.host_instance
   AND jsonb_typeof(p_evidence -> 'launch_token') = 'string'
   AND (p_evidence ->> 'launch_token') = p_operation.launch_token
   AND (p_evidence -> 'launch_token_absent') = 'true'::jsonb;

CREATE OR REPLACE FUNCTION public.capture_recovery_authorized(
    p_owner public.worker_incarnations,
    p_replacement public.worker_incarnations
) RETURNS boolean
LANGUAGE sql
IMMUTABLE
SET search_path = ''
RETURN p_owner.state = 'terminated'
   AND p_owner.terminated_at IS NOT NULL
   AND p_owner.outcome IS NOT NULL
   AND p_owner.authority_kind = p_replacement.authority_kind
   AND (
       public.capture_worker_host_instance(
           p_owner.authority_kind, p_owner.authority_binding
       ) = public.capture_worker_host_instance(
           p_replacement.authority_kind, p_replacement.authority_binding
       )
       OR (
           public.capture_worker_authority_scope(
               p_owner.authority_kind, p_owner.authority_binding
           ) IS NOT NULL
           AND public.capture_worker_authority_scope(
               p_owner.authority_kind, p_owner.authority_binding
           ) = public.capture_worker_authority_scope(
               p_replacement.authority_kind, p_replacement.authority_binding
           )
       )
   );
"""

_HAZARDOUS = [
    "capture_launch_abort_evidence_valid(capture_operations,jsonb)",
    "capture_recovery_authorized(worker_incarnations,worker_incarnations)",
]


def _hazards(conn: psycopg.Connection) -> list[str]:
    return [row[0] for row in conn.execute(_HAZARD_SQL).fetchall()]


def _add_defaulted_columns(conn: psycopg.Connection) -> None:
    """Both tables accept a defaulted ADD COLUMN; the probe columns are rolled back."""
    with conn.transaction(force_rollback=True):
        conn.execute("ALTER TABLE capture_operations ADD COLUMN probe_3612 text DEFAULT 'x'")
        conn.execute("ALTER TABLE worker_incarnations ADD COLUMN probe_3612 text DEFAULT 'x'")


def test_supersession_literals_are_well_formed() -> None:
    entries = supersessions.SUPERSESSIONS
    assert [entry.version for entry in entries] == ["0112"]
    assert len({entry.version for entry in entries}) == len(entries)
    for entry in entries:
        assert entry.filename.startswith(f"{entry.version}_")
        for digest in (entry.superseded_sha256, entry.current_sha256):
            assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")
        assert entry.superseded_sha256 != entry.current_sha256
        on_disk = (migrate.SCHEMA_DIR / entry.filename).read_bytes()
        assert hashlib.sha256(on_disk).hexdigest() == entry.current_sha256
        assert entry.decision == "ADR-0818"


def test_no_new_style_function_pins_a_table_row_type(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    assert _hazards(pg_conn) == []
    _add_defaulted_columns(pg_conn)


def test_the_hazard_query_sees_arrays_and_out_parameters(pg_conn: psycopg.Connection) -> None:
    pg_conn.execute(
        "CREATE TABLE hz (id int);"
        "CREATE FUNCTION hz_arr(p hz[]) RETURNS int LANGUAGE sql RETURN cardinality(p);"
        "CREATE FUNCTION hz_out(i int, OUT a int, OUT b hz) LANGUAGE sql"
        " BEGIN ATOMIC SELECT i, ROW(i)::hz; END;"
        "CREATE FUNCTION hz_ok(p int[]) RETURNS int LANGUAGE sql RETURN cardinality(p);"
    )
    assert _hazards(pg_conn) == ["hz_arr(hz[])", "hz_out(integer)"]


def test_an_existing_database_upgrades_through_0199(
    pg_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    every = migrate.discover_migrations()
    before = [m for m in every if m.version < "0199"]
    monkeypatch.setattr(migrate, "discover_migrations", lambda: before)
    migrate.apply_migrations(pg_conn)
    (entry,) = supersessions.SUPERSESSIONS
    pg_conn.execute(_ORIGINAL_NEW_STYLE)
    pg_conn.execute(
        "UPDATE schema_migrations SET checksum = %s WHERE version = '0112'",
        (entry.superseded_sha256,),
    )
    assert _hazards(pg_conn) == _HAZARDOUS
    if pg_conn.info.server_version >= 170012:  # the rule PostgreSQL 17.12 introduced
        with pytest.raises(psycopg.errors.FeatureNotSupported, match="uses its row type"):
            _add_defaulted_columns(pg_conn)

    through_0199 = [m for m in every if m.version <= "0199"]
    monkeypatch.setattr(migrate, "discover_migrations", lambda: through_0199)
    assert migrate.apply_migrations(pg_conn) == ["0199"]

    assert _hazards(pg_conn) == []
    _add_defaulted_columns(pg_conn)
