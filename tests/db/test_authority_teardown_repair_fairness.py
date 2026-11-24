"""Real-Postgres retry ordering and write fences for authority teardown (ADR-0838)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.sql import SQL
from psycopg.sql import Literal as SqlLiteral

import kdive.services.systems.reclaim as core_reclaim
from kdive.db import migrate
from kdive.reconciler.repairs.jobs import repair_terminal_authority_system_attempts
from tests.conftest import _RoleDsns
from tests.db.authority_system_support import (
    _seed_direct_authority_case,
    _seed_direct_terminal_attempt,
)
from tests.db.external_boot_authority_support import _apply_through, _apply_version

_Candidate = tuple[UUID, int, UUID, int, str, str]
_MARK = "SELECT mark_authority_system_teardown_repair_retry(%s,%s,%s,%s,%s,%s)"
_SIGNATURE = "mark_authority_system_teardown_repair_retry(uuid,bigint,uuid,bigint,text,text)"


class _Store:
    def delete_retired_key_batch(self, key: str, limit: int) -> bool:
        return True


@pytest.fixture(autouse=True)
def _owned_pcap_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(core_reclaim, "pcap_dir", lambda system_id: tmp_path / str(system_id))


@pytest.fixture(autouse=True)
def _owned_sql_faults(migrated_url: str) -> Iterator[None]:
    # The database fixture resets rows between tests, so fault DDL needs its own teardown.
    try:
        yield
    finally:
        with psycopg.connect(migrated_url) as conn:
            conn.execute("DROP TRIGGER IF EXISTS block_repair_cleanup ON system_bootstrap_keys")
            conn.execute("DROP TRIGGER IF EXISTS reject_retry_marker ON authority_system_attempts")
            conn.execute("DROP FUNCTION IF EXISTS block_repair_cleanup()")
            conn.execute("DROP FUNCTION IF EXISTS reject_retry_marker()")
            conn.execute("DROP TABLE IF EXISTS blocked_repair_systems")


def _seed_receipt(conn: psycopg.Connection, *, receipt_at: datetime | None = None) -> _Candidate:
    case = _seed_direct_authority_case(
        conn, operation="preactivation-teardown", ownership_state="teardown-requested"
    )
    authority_id, _, _, journal, receipt = _seed_direct_terminal_attempt(
        conn, case, disposition="preactivation-absent", receipt_at=receipt_at
    )
    conn.execute(
        "UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
        (case["job_id"],),
    )
    return cast(UUID, authority_id), 1, cast(UUID, case["system_id"]), 2, journal, receipt


def _snapshot(conn: psycopg.Connection, candidate: _Candidate) -> tuple[object, ...]:
    row = conn.execute(
        "SELECT to_jsonb(a),to_jsonb(o),to_jsonb(j),to_jsonb(s) "
        "FROM authority_system_attempts a JOIN authority_system_ownership o "
        "ON o.system_id=a.system_id JOIN jobs j ON j.id=a.job_id "
        "JOIN systems s ON s.id=a.system_id WHERE a.id=%s",
        (candidate[0],),
    ).fetchone()
    assert row is not None
    return row


def _repair(dsn: str) -> int:
    async def run() -> int:
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            return await repair_terminal_authority_system_attempts(conn, _Store())

    return asyncio.run(run())


def _block_cleanup(
    conn: psycopg.Connection, candidates: list[_Candidate], mode: Literal["raise", "retain"]
) -> None:
    conn.execute("CREATE TABLE blocked_repair_systems (system_id uuid PRIMARY KEY)")
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO blocked_repair_systems VALUES (%s)", [(row[2],) for row in candidates]
        )
    conn.execute("""
        CREATE FUNCTION block_repair_cleanup() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.blocked_repair_systems WHERE system_id=OLD.system_id)
            THEN
                IF TG_ARGV[0]='raise' THEN
                    RAISE EXCEPTION 'injected retryable cleanup failure';
                END IF;
                RETURN NULL;
            END IF;
            RETURN OLD;
        END $$
    """)
    conn.execute(
        SQL(
            "CREATE TRIGGER block_repair_cleanup BEFORE DELETE ON system_bootstrap_keys "
            "FOR EACH ROW EXECUTE FUNCTION block_repair_cleanup({})"
        ).format(SqlLiteral(mode))
    )


@pytest.mark.parametrize("mode", ["raise", "retain"])
@pytest.mark.parametrize("delayed", [False, True])
def test_full_retry_page_yields_to_a_healthy_receipt(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    mode: Literal["raise", "retain"],
    delayed: bool,
) -> None:
    base = datetime.now(UTC) - timedelta(days=2)
    with psycopg.connect(migrated_url) as conn:
        candidates = [
            _seed_receipt(conn, receipt_at=base + timedelta(microseconds=i)) for i in range(101)
        ]
        _block_cleanup(conn, candidates[:100], mode)
    dsn = authority_role_dsns("kdive_reconciler")
    assert _repair(dsn) == 0
    if delayed:
        # Model a late next pass by aging retry times, while retaining their order after receipts.
        with psycopg.connect(migrated_url) as conn:
            conn.execute(
                "UPDATE authority_system_attempts SET teardown_repair_last_retry_at="
                "clock_timestamp()-interval '1 hour' "
                "WHERE teardown_repair_last_retry_at IS NOT NULL"
            )
    repaired = _repair(dsn)
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT consumed_at IS NOT NULL FROM authority_system_attempts WHERE id=%s",
            (candidates[-1][0],),
        ).fetchone() == (True,), "the first100 retrying receipts starved a healthy later receipt"
        assert repaired == 1
        assert conn.execute(
            "SELECT count(*) FROM authority_system_attempts WHERE consumed_at IS NULL "
            "AND teardown_repair_last_retry_at IS NOT NULL"
        ).fetchone() == (100,)
        conn.execute("DELETE FROM blocked_repair_systems")
    with psycopg.connect(dsn) as conn:
        retained = conn.execute(
            "SELECT * FROM list_authority_system_teardown_repairs(100)"
        ).fetchall()
        assert {row[0] for row in retained} == {row[0] for row in candidates[:100]}
    assert _repair(dsn) == 100
    with psycopg.connect(dsn) as conn:
        assert (
            conn.execute("SELECT * FROM list_authority_system_teardown_repairs(100)").fetchall()
            == []
        )


@pytest.mark.parametrize("eligibility", ["expired", "job-ended", "worker-ended"])
def test_retry_marker_changes_only_the_timestamp(
    migrated_url: str, authority_role_dsns: _RoleDsns, eligibility: str
) -> None:
    with psycopg.connect(migrated_url) as conn:
        candidate = _seed_receipt(conn)
        if eligibility != "expired":
            conn.execute(
                "UPDATE jobs SET lease_expires_at=clock_timestamp()+interval '1 hour' "
                "WHERE id=(SELECT job_id FROM authority_system_attempts WHERE id=%s)",
                (candidate[0],),
            )
            if eligibility == "job-ended":
                conn.execute(
                    "UPDATE jobs SET state='failed' WHERE id="
                    "(SELECT job_id FROM authority_system_attempts WHERE id=%s)",
                    (candidate[0],),
                )
            else:
                conn.execute(
                    "UPDATE worker_incarnations SET state='terminated',"
                    "terminated_at=clock_timestamp(),outcome='killed' WHERE incarnation="
                    "(SELECT worker_incarnation FROM authority_system_attempts WHERE id=%s)",
                    (candidate[0],),
                )
        before = _snapshot(conn, candidate)
    assert cast(dict, before[0])["teardown_repair_last_retry_at"] is None
    with psycopg.connect(authority_role_dsns("kdive_reconciler")) as conn:
        assert conn.execute(_MARK, candidate).fetchone() == (True,)
    with psycopg.connect(migrated_url) as conn:
        after = _snapshot(conn, candidate)
    attempt = cast(dict, after[0])
    assert attempt.pop("teardown_repair_last_retry_at") is not None
    original = cast(dict, before[0])
    original.pop("teardown_repair_last_retry_at")
    assert after == before


@pytest.mark.parametrize("field", range(6))
def test_retry_marker_refuses_each_stale_identity_field(
    migrated_url: str, authority_role_dsns: _RoleDsns, field: int
) -> None:
    with psycopg.connect(migrated_url) as conn:
        candidate = _seed_receipt(conn)
        before = _snapshot(conn, candidate)
    wrong: list[object] = list(candidate)
    wrong[field] = (
        uuid4()
        if field in (0, 2)
        else cast(int, wrong[field]) + 1
        if field in (1, 3)
        else "sha256:" + "f" * 64
    )
    with psycopg.connect(authority_role_dsns("kdive_reconciler")) as conn:
        assert conn.execute(_MARK, wrong).fetchone() == (False,)
    with psycopg.connect(migrated_url) as conn:
        assert _snapshot(conn, candidate) == before


@pytest.mark.parametrize(
    "change",
    [
        "consumed",
        "owner-head",
        "owner-digest",
        "attempt-state",
        "owner-state",
        "system-state",
        "live-lease",
        "missing-head",
    ],
)
def test_retry_marker_rechecks_current_eligibility(
    migrated_url: str, authority_role_dsns: _RoleDsns, change: str
) -> None:
    with psycopg.connect(migrated_url) as conn:
        candidate = _seed_receipt(conn)
        if change == "consumed":
            conn.execute(
                "UPDATE authority_system_attempts SET consumed_at=clock_timestamp() WHERE id=%s",
                (candidate[0],),
            )
        elif change == "owner-head":
            conn.execute(
                "UPDATE authority_system_ownership SET journal_sequence=3 WHERE system_id=%s",
                (candidate[2],),
            )
        elif change == "owner-digest":
            conn.execute(
                "UPDATE authority_system_ownership SET journal_digest=%s WHERE system_id=%s",
                ("sha256:" + "f" * 64, candidate[2]),
            )
        elif change == "attempt-state":
            conn.execute(
                "UPDATE authority_system_attempts SET "
                "state='superseded',superseded_at=clock_timestamp() WHERE id=%s",
                (candidate[0],),
            )
        elif change == "owner-state":
            conn.execute(
                "UPDATE authority_system_ownership SET state='torn-down' WHERE system_id=%s",
                (candidate[2],),
            )
        elif change == "system-state":
            conn.execute("UPDATE systems SET state='torn_down' WHERE id=%s", (candidate[2],))
        elif change == "live-lease":
            conn.execute(
                "UPDATE jobs SET lease_expires_at=clock_timestamp()+interval '1 hour' WHERE "
                "id=(SELECT job_id FROM authority_system_attempts WHERE id=%s)",
                (candidate[0],),
            )
        else:
            conn.execute(
                "UPDATE authority_system_ownership SET current_attempt_id=NULL WHERE system_id=%s",
                (candidate[2],),
            )
        before = _snapshot(conn, candidate)
    with psycopg.connect(authority_role_dsns("kdive_reconciler")) as conn:
        assert conn.execute(_MARK, candidate).fetchone() == (False,)
    with psycopg.connect(migrated_url) as conn:
        assert _snapshot(conn, candidate) == before


@pytest.mark.parametrize("field", range(6))
def test_retry_marker_rejects_null_identity(
    migrated_url: str, authority_role_dsns: _RoleDsns, field: int
) -> None:
    with psycopg.connect(migrated_url) as conn:
        candidate: list[object] = list(_seed_receipt(conn))
    candidate[field] = None
    with (
        psycopg.connect(authority_role_dsns("kdive_reconciler")) as conn,
        pytest.raises(psycopg.errors.InvalidParameterValue, match="identity"),
    ):
        conn.execute(_MARK, candidate)


@pytest.mark.parametrize("field,value", [(1, 0), (3, 0), (4, "bad-digest"), (5, "bad-digest")])
def test_retry_marker_rejects_invalid_identity(
    migrated_url: str, authority_role_dsns: _RoleDsns, field: int, value: object
) -> None:
    with psycopg.connect(migrated_url) as conn:
        candidate: list[object] = list(_seed_receipt(conn))
    candidate[field] = value
    with (
        psycopg.connect(authority_role_dsns("kdive_reconciler")) as conn,
        pytest.raises(psycopg.errors.InvalidParameterValue, match="identity"),
    ):
        conn.execute(_MARK, candidate)


def test_retry_marker_refuses_non_absence_receipt(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    with psycopg.connect(migrated_url) as conn:
        case = _seed_direct_authority_case(
            conn, operation="preactivation-teardown", ownership_state="teardown-requested"
        )
        authority_id, _, _, journal, receipt = _seed_direct_terminal_attempt(
            conn, case, disposition="retained-quarantine"
        )
        conn.execute(
            "UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
            (case["job_id"],),
        )
        candidate = (
            cast(UUID, authority_id),
            1,
            cast(UUID, case["system_id"]),
            2,
            journal,
            receipt,
        )
        before = _snapshot(conn, candidate)
    with psycopg.connect(authority_role_dsns("kdive_reconciler")) as conn:
        assert conn.execute(_MARK, candidate).fetchone() == (False,)
    with psycopg.connect(migrated_url) as conn:
        assert _snapshot(conn, candidate) == before


def test_retry_marker_is_reconciler_only(migrated_url: str, authority_role_dsns: _RoleDsns) -> None:
    with psycopg.connect(migrated_url) as admin:
        candidate = _seed_receipt(admin)
        metadata = admin.execute(
            "SELECT prosecdef,proconfig FROM pg_proc WHERE oid=%s::regprocedure", (_SIGNATURE,)
        ).fetchone()
        assert metadata == (True, ['search_path=""'])
        for role, login in authority_role_dsns.logins.items():
            assert admin.execute(
                "SELECT has_function_privilege(%s,%s,'EXECUTE')", (login, _SIGNATURE)
            ).fetchone() == (role == "kdive_reconciler",)
            assert admin.execute(
                "SELECT has_table_privilege(%s,'authority_system_attempts','UPDATE')", (login,)
            ).fetchone() == (False,)
        assert admin.execute(
            "SELECT count(*) FROM pg_proc p,LATERAL aclexplode(p.proacl) a WHERE "
            "p.oid=%s::regprocedure AND a.grantee=0",
            (_SIGNATURE,),
        ).fetchone() == (0,)
    for role in ("kdive_server", "kdive_worker", "kdive_provider_authority"):
        with (
            psycopg.connect(authority_role_dsns(role)) as actor,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            actor.execute(_MARK, candidate)
    # A mistakenly widened function ACL still cannot bypass its session-role check.
    with psycopg.connect(migrated_url) as admin:
        admin.execute(SQL("GRANT EXECUTE ON FUNCTION {} TO kdive_server").format(SQL(_SIGNATURE)))
    try:
        with (
            psycopg.connect(authority_role_dsns("kdive_server")) as actor,
            pytest.raises(psycopg.errors.InsufficientPrivilege, match="reconciler"),
        ):
            actor.execute(_MARK, candidate)
    finally:
        with psycopg.connect(migrated_url) as admin:
            admin.execute(
                SQL("REVOKE EXECUTE ON FUNCTION {} FROM kdive_server").format(SQL(_SIGNATURE))
            )


def test_marker_failure_does_not_starve_a_selected_healthy_sibling(
    migrated_url: str, authority_role_dsns: _RoleDsns, caplog: pytest.LogCaptureFixture
) -> None:
    with psycopg.connect(migrated_url) as conn:
        candidates = [_seed_receipt(conn) for _ in range(3)]
        _block_cleanup(conn, candidates[:2], "raise")
        conn.execute("""CREATE FUNCTION reject_retry_marker() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'injected retry marker failure'; END $$""")
        conn.execute(
            "CREATE TRIGGER reject_retry_marker BEFORE UPDATE OF teardown_repair_last_retry_at ON"
            " authority_system_attempts FOR EACH ROW EXECUTE FUNCTION reject_retry_marker()"
        )
    assert _repair(authority_role_dsns("kdive_reconciler")) == 1
    assert sum("retry ordering" in row.getMessage() for row in caplog.records) == 2
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT consumed_at IS NOT NULL FROM authority_system_attempts WHERE id=%s",
            (candidates[-1][0],),
        ).fetchone() == (True,)
        assert conn.execute(
            "SELECT count(*) FROM authority_system_attempts WHERE consumed_at IS NULL AND "
            "teardown_repair_last_retry_at IS NULL"
        ).fetchone() == (2,)


def test_retry_marker_waits_for_system_lock_and_rechecks_afterward(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    with psycopg.connect(migrated_url) as conn:
        candidate = _seed_receipt(conn)
    dsn = authority_role_dsns("kdive_reconciler")
    with (
        psycopg.connect(dsn) as contender,
        psycopg.connect(migrated_url) as blocker,
        psycopg.connect(migrated_url, autocommit=True) as observer,
    ):
        blocker.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('kdive:system:' || "
            "%s::uuid::text,2125))",
            (candidate[2],),
        )
        backend = contender.info.backend_pid
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(lambda: contender.execute(_MARK, candidate).fetchone())
            try:
                deadline = time.monotonic() + 5
                while (
                    observer.execute(
                        "SELECT 1 FROM pg_locks WHERE pid=%s AND locktype='advisory' AND NOT "
                        "granted",
                        (backend,),
                    ).fetchone()
                    is None
                ):
                    if future.done():
                        future.result()
                        raise AssertionError("marker did not wait for the System lock")
                    assert time.monotonic() < deadline, "marker never reached the System lock"
                    time.sleep(0.01)
                blocker.execute(
                    "UPDATE authority_system_attempts SET consumed_at=clock_timestamp() WHERE "
                    "id=%s",
                    (candidate[0],),
                )
                blocker.commit()
                assert future.result(timeout=5) == (False,)
            finally:
                blocker.rollback()
                future.result(timeout=5)
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT teardown_repair_last_retry_at FROM authority_system_attempts WHERE id=%s",
            (candidate[0],),
        ).fetchone() == (None,)


def test_migration_0205_is_registered_once() -> None:
    assert [
        (m.version, m.filename) for m in migrate.discover_migrations() if m.version == "0205"
    ] == [("0205", "0205_authority_teardown_repair_fairness.sql")]


@pytest.mark.parametrize("shape", ["missing", "duplicated"])
def test_retry_migration_rejects_changed_selector_shape(
    pg_conn: psycopg.Connection, shape: str
) -> None:
    _apply_through(pg_conn, "0204")
    signature = "list_authority_system_teardown_repairs(integer)"
    query = "SELECT pg_get_functiondef(%s::regprocedure)"
    row = pg_conn.execute(query, (signature,)).fetchone()
    assert row is not None
    original = row[0]
    seam = "    ORDER BY attempt.receipt_at,attempt.id"
    assert original.count(seam) == 1
    changed = original.replace(
        seam,
        "    ORDER BY attempt.id,attempt.receipt_at"
        if shape == "missing"
        else "/* " + seam + " */\n" + seam,
    )
    pg_conn.execute(changed.encode())
    row = pg_conn.execute(query, (signature,)).fetchone()
    assert row is not None
    before = row[0]
    with (
        pytest.raises(psycopg.errors.RaiseException, match="repair ordering shape changed"),
        pg_conn.transaction(),
    ):
        _apply_version(pg_conn, "0205")
    row = pg_conn.execute(query, (signature,)).fetchone()
    assert row is not None and row[0] == before
    assert (
        pg_conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name='authority_system_attempts' "
            "AND column_name='teardown_repair_last_retry_at'"
        ).fetchone()
        is None
    )


def test_retry_migration_preserves_selector_metadata_and_finalizer(
    pg_conn: psycopg.Connection,
) -> None:
    _apply_through(pg_conn, "0204")
    selector = "list_authority_system_teardown_repairs(integer)"
    finalizer = "finalize_authority_system_teardown_repair(uuid,bigint,uuid,bigint,text,text)"
    query = (
        "SELECT pg_get_functiondef(oid),proowner,prosecdef,proconfig,proacl,"
        "proargtypes,prorettype,proargnames,provolatile FROM pg_proc "
        "WHERE oid=%s::regprocedure"
    )
    before = pg_conn.execute(query, (selector,)).fetchone()
    finalize_before = pg_conn.execute(query, (finalizer,)).fetchone()
    _apply_version(pg_conn, "0205")
    after = pg_conn.execute(query, (selector,)).fetchone()
    assert before is not None and after is not None and finalize_before is not None
    assert after[1:] == before[1:]
    assert after[0] == before[0].replace(
        "    ORDER BY attempt.receipt_at,attempt.id",
        "    ORDER BY COALESCE(attempt.teardown_repair_last_retry_at,attempt.receipt_at),"
        "attempt.receipt_at,attempt.id",
    )
    assert pg_conn.execute(query, (finalizer,)).fetchone() == finalize_before
