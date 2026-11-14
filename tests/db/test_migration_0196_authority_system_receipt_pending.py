"""Real-Postgres proofs of the pending absence-receipt predicate (#3627, ADR-0620)."""

from __future__ import annotations

import threading
import time
from typing import Any, LiteralString
from uuid import uuid4

import psycopg
import pytest

from kdive.db import migrate
from tests.db.authority_system_support import (
    _seed_direct_authority_case,
    _seed_direct_terminal_attempt,
)
from tests.db.external_boot_authority_support import _RoleDsns

_PENDING = "SELECT authority_system_preactivation_receipt_pending(%s,%s)"
_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended('kdive:system:' || %s::text, 2125))"
_DEVIATIONS: dict[str, LiteralString | None] = {
    "pending": None,
    "other-job": None,
    "consumed": "UPDATE authority_system_attempts SET consumed_at=clock_timestamp() "
    "WHERE system_id=%s",
    "not-teardown-requested": "UPDATE authority_system_ownership SET state='provisioning' "
    "WHERE system_id=%s",
    "head-moved": "UPDATE authority_system_ownership SET journal_sequence=3 WHERE system_id=%s",
    "not-current": "UPDATE authority_system_ownership SET current_attempt_id=NULL "
    "WHERE system_id=%s",
}


def _case(migrated_url: str, disposition: str = "preactivation-absent") -> dict[str, Any]:
    with psycopg.connect(migrated_url) as admin:
        case: dict[str, Any] = _seed_direct_authority_case(
            admin,
            operation="preactivation-teardown",
            ownership_state="teardown-requested",
            attempt=3,
        )
        _seed_direct_terminal_attempt(admin, case, disposition=disposition)
        admin.execute(
            "UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 minute' WHERE id=%s",
            (case["job_id"],),
        )
    return case


def test_migration_0196_is_registered_once() -> None:
    assert [
        (migration.version, migration.filename)
        for migration in migrate.discover_migrations()
        if migration.version == "0196"
    ] == [("0196", "0196_authority_system_receipt_pending.sql")]


@pytest.mark.parametrize("deviation", list(_DEVIATIONS))
def test_pending_receipt_matches_only_the_repair_predicate(
    migrated_url: str, authority_role_dsns: _RoleDsns, deviation: str
) -> None:
    case = _case(migrated_url)
    statement = _DEVIATIONS[deviation]
    if statement is not None:
        with psycopg.connect(migrated_url) as admin:
            admin.execute(statement, (case["system_id"],))
    job_id = uuid4() if deviation == "other-job" else case["job_id"]
    with psycopg.connect(authority_role_dsns("kdive_server")) as server:
        assert server.execute(_PENDING, (case["system_id"], job_id)).fetchone() == (
            deviation == "pending",
        )


def test_other_disposition_and_unknown_system_are_not_pending(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case = _case(migrated_url, disposition="retained-quarantine")
    with psycopg.connect(authority_role_dsns("kdive_server")) as server:
        assert server.execute(_PENDING, (case["system_id"], case["job_id"])).fetchone() == (False,)
        assert server.execute(_PENDING, (uuid4(), uuid4())).fetchone() == (False,)


@pytest.mark.parametrize(
    ("role", "allowed"),
    [
        ("kdive_server", True),
        ("kdive_reconciler", False),
        ("kdive_worker", False),
        ("kdive_provider_authority", False),
    ],
)
def test_only_the_server_may_execute(
    authority_role_dsns: _RoleDsns, role: str, allowed: bool
) -> None:
    with psycopg.connect(authority_role_dsns(role), autocommit=True) as conn:
        if allowed:
            assert conn.execute(_PENDING, (uuid4(), uuid4())).fetchone() == (False,)
            return
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(_PENDING, (uuid4(), uuid4()))


def _await_lock_wait(migrated_url: str, pid: int) -> None:
    with psycopg.connect(migrated_url, autocommit=True) as observer:
        for _ in range(200):
            row = observer.execute(
                "SELECT count(*) FROM pg_locks WHERE pid=%s AND NOT granted", (pid,)
            ).fetchone()
            if row == (1,):
                return
            time.sleep(0.05)
    raise AssertionError("the server call never waited on the System lock")


def test_answers_after_the_system_lock_holder_commits(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case = _case(migrated_url)
    params = (case["system_id"], case["job_id"])
    answers: list[Any] = []
    with (
        psycopg.connect(migrated_url) as holder,
        psycopg.connect(authority_role_dsns("kdive_server")) as server,
    ):
        holder.execute(_LOCK, (case["system_id"],))
        waiter = threading.Thread(
            target=lambda: answers.append(server.execute(_PENDING, params).fetchone())
        )
        waiter.start()
        _await_lock_wait(migrated_url, server.info.backend_pid)
        holder.execute(
            "UPDATE authority_system_attempts SET consumed_at=clock_timestamp() WHERE system_id=%s",
            (case["system_id"],),
        )
        holder.commit()
        waiter.join(timeout=10)
    assert answers == [(False,)]
