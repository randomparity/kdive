"""Real-Postgres proof that settlement ends the stranded boot job (#3596, ADR-0816)."""

from __future__ import annotations

from typing import Any, LiteralString, cast
from uuid import UUID

import psycopg
import pytest

from kdive.db import migrate
from tests.db.external_boot_authority_support import _RoleDsns
from tests.db.stranded_activation_support import _record_both, _strand, _Stranded

_SETTLE = "SELECT settle_stranded_external_boot_activation(%s,%s,%s,%s,%s)"
_SETTLE_ARGS = ("admin", "session", "operator-cli", "stranded by #3596")


def test_migration_0198_is_registered() -> None:
    assert "0198" in [item.version for item in migrate.discover_migrations()]


def _exhausted(migrated_url: str, role_dsns: _RoleDsns) -> _Stranded:
    """The live strand: both owners' evidence, and the boot job running past its last attempt."""
    stranded = _strand(migrated_url, role_dsns)
    _record_both(role_dsns, stranded)
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "UPDATE jobs SET state = 'running', attempt = max_attempts, worker_id = %s, "
            "heartbeat_at = now() - interval '10 minutes', "
            "lease_expires_at = now() - interval '5 minutes' WHERE id = %s",
            (stranded.case.worker_id, stranded.case.job_id),
        )
        conn.execute("UPDATE runs SET state = 'running' WHERE id = %s", (stranded.case.run_id,))
    return stranded


def _settle(role_dsns: _RoleDsns, activation_id: UUID) -> str:
    with psycopg.connect(role_dsns("kdive_server"), autocommit=True) as conn:
        row = conn.execute(_SETTLE, (activation_id, *_SETTLE_ARGS)).fetchone()
    assert row is not None
    return row[0]


def _rows(migrated_url: str, stranded: _Stranded) -> tuple[Any, ...]:
    case = stranded.case
    with psycopg.connect(migrated_url) as conn:
        row = conn.execute(
            "SELECT j.state, j.error_category, j.attempt, j.lease_expires_at, r.state, "
            "r.failure_category, (SELECT count(*) FROM platform_audit_log "
            "WHERE scope = 'failed_job:' || j.id::text) "
            "FROM jobs j, runs r WHERE j.id = %s AND r.id = %s",
            (case.job_id, case.run_id),
        ).fetchone()
    assert row is not None
    return row


def test_settlement_fails_the_exhausted_job_and_its_run(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    stranded = _exhausted(migrated_url, authority_role_dsns)

    assert _settle(authority_role_dsns, stranded.case.activation_id) == "applied"

    ended = _rows(migrated_url, stranded)
    assert ended[:2] + ended[4:] == ("failed", "lease_expired", "failed", "lease_expired", 1)
    assert _settle(authority_role_dsns, stranded.case.activation_id) == "replayed"
    assert _rows(migrated_url, stranded)[-1] == 1


_UNCHANGED = {
    "live-lease": "UPDATE jobs SET lease_expires_at = now() + interval '1 hour' WHERE id = {job}",
    "attempts-remaining": "UPDATE jobs SET max_attempts = attempt + 1 WHERE id = {job}",
    "queued": "UPDATE jobs SET state = 'queued', worker_id = NULL, lease_expires_at = NULL, "
    "heartbeat_at = NULL WHERE id = {job}",
    "another-activation": "UPDATE jobs SET payload = jsonb_set(payload, "
    "'{{external_boot_authority_v1,activation_id}}', to_jsonb(gen_random_uuid()::text)) "
    "WHERE id = {job}",
    "later-refusal": "UPDATE external_boot_activation_settlements "
    "SET worker_recorded_at = now() - interval '2 hours' WHERE activation_id = {activation}",
}


@pytest.mark.parametrize("shape", sorted(_UNCHANGED))
def test_a_held_job_or_a_refusal_changes_nothing(
    migrated_url: str, authority_role_dsns: _RoleDsns, shape: str
) -> None:
    stranded = _exhausted(migrated_url, authority_role_dsns)
    case = stranded.case
    sql = _UNCHANGED[shape].format(job=f"'{case.job_id}'", activation=f"'{case.activation_id}'")
    expected = "evidence_stale" if shape == "later-refusal" else "job_live"
    with psycopg.connect(migrated_url) as conn:
        conn.execute(cast(LiteralString, sql))
    before = _rows(migrated_url, stranded)

    assert _settle(authority_role_dsns, case.activation_id) == expected

    assert _rows(migrated_url, stranded) == before


def test_a_claimable_grant_keeps_the_job_live(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    """An unconsumed acknowledged-retry grant lets a worker claim the job again (0150)."""
    stranded = _exhausted(migrated_url, authority_role_dsns)
    # The predicate is private to definer functions; stand it in for one transaction, rolled
    # back, so no other test sees it. A superuser session passes the server-role check.
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "CREATE OR REPLACE FUNCTION public.has_acknowledged_external_boot_retry_proof("
            "p_job public.jobs) RETURNS boolean LANGUAGE sql STABLE RETURNS NULL ON NULL INPUT "
            "SET search_path = '' AS 'SELECT true'"
        )
        row = conn.execute(_SETTLE, (stranded.case.activation_id, *_SETTLE_ARGS)).fetchone()
        conn.rollback()
    assert row == ("job_live",)
    assert _rows(migrated_url, stranded)[0] == "running"


def test_a_locked_job_refuses_without_waiting(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    stranded = _exhausted(migrated_url, authority_role_dsns)
    with psycopg.connect(migrated_url) as holder:
        holder.execute("SELECT 1 FROM jobs WHERE id = %s FOR UPDATE", (stranded.case.job_id,))
        # A settlement that waited would raise LockNotAvailable here instead of hanging.
        with psycopg.connect(authority_role_dsns("kdive_server"), autocommit=True) as conn:
            conn.execute("SET lock_timeout = '2s'")
            row = conn.execute(_SETTLE, (stranded.case.activation_id, *_SETTLE_ARGS)).fetchone()
        holder.rollback()
    assert row == ("job_live",)
    assert _rows(migrated_url, stranded)[0] == "running"
