"""Claimable depth honors the existing authority receipt handoff (ADR-0846)."""

from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

import psycopg
import pytest

from kdive.providers.system_authority.protocol import AuthoritySystemOperation
from tests.db.authority_system_support import (
    _seed_direct_authority_case,
    _seed_direct_terminal_attempt,
)
from tests.db.external_boot_authority_support import _apply_through, _apply_version

_COUNT = "SELECT count_claimable_worker_jobs(%s::text[])"
_CLAIM = "SELECT id FROM claim_worker_job(%s,%s,interval '1 minute',%s::text[])"
_JOB = "SELECT to_jsonb(j) FROM jobs j WHERE id=%s"
_RECEIPTS = "SELECT to_jsonb(a) FROM authority_system_attempts a WHERE job_id=%s ORDER BY id"


@pytest.mark.parametrize("operation", list(AuthoritySystemOperation))
@pytest.mark.parametrize("state", ["queued", "running"])
@pytest.mark.parametrize("receipt", ["pending", "consumed", "absent"])
def test_receipt_depth_matches_claim(
    migrated_url: str,
    authority_role_dsns: Callable[[str], str],
    state: str,
    receipt: str,
    operation: AuthoritySystemOperation,
) -> None:
    lane = f"receipt-{uuid4()}"
    with psycopg.connect(migrated_url, autocommit=True) as admin:
        case = _seed_direct_authority_case(
            admin,
            operation=operation,
            ownership_state=(
                "provisioning"
                if operation is AuthoritySystemOperation.PROVISION
                else "teardown-requested"
            ),
        )
        if receipt != "absent":
            _seed_direct_terminal_attempt(admin, case, disposition="preactivation-absent")
            if receipt == "consumed":
                admin.execute(
                    "UPDATE authority_system_attempts SET consumed_at=clock_timestamp() "
                    "WHERE job_id=%s",
                    (case["job_id"],),
                )
        admin.execute(
            "UPDATE jobs SET state=%s,dispatch_lane=%s,"
            "lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
            (state, lane, case["job_id"]),
        )
        before = admin.execute(_JOB, (case["job_id"],)).fetchone()
        receipts = admin.execute(_RECEIPTS, (case["job_id"],)).fetchall()
        with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
            depth = worker.execute(_COUNT, ([lane],)).fetchone()
            assert admin.execute(_JOB, (case["job_id"],)).fetchone() == before
            assert admin.execute(_RECEIPTS, (case["job_id"],)).fetchall() == receipts
            claimed = worker.execute(
                _CLAIM, (case["worker"], case["credential"], [lane])
            ).fetchall()
        eligible = receipt != "pending"
        assert claimed == ([(case["job_id"],)] if eligible else [])
        assert depth == (int(eligible),)


def test_other_receipt_keeps_control_claimable(
    migrated_url: str,
    authority_role_dsns: Callable[[str], str],
) -> None:
    lane = f"receipt-{uuid4()}"
    with psycopg.connect(migrated_url, autocommit=True) as admin:
        pending = _seed_direct_authority_case(
            admin, operation="provision", ownership_state="provisioning"
        )
        _seed_direct_terminal_attempt(admin, pending, disposition="preactivation-absent")
        control = _seed_direct_authority_case(
            admin, operation="provision", ownership_state="provisioning"
        )
        admin.execute(
            "UPDATE jobs SET state='queued',dispatch_lane=%s WHERE id=ANY(%s::uuid[])",
            (lane, [pending["job_id"], control["job_id"]]),
        )
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        assert worker.execute(_COUNT, ([lane],)).fetchone() == (1,)
        assert worker.execute(
            _CLAIM, (control["worker"], control["credential"], [lane])
        ).fetchall() == [
            (control["job_id"],),
        ]


def test_upgrade_preserves_function_authority_and_rejects_reapplication(
    pg_conn: psycopg.Connection,
) -> None:
    _apply_through(pg_conn, "0205")
    claim = "public.claim_worker_job(text,bytea,interval,text[])"
    count = "public.count_claimable_worker_jobs(text[])"
    metadata = "SELECT to_jsonb(p)-'prosrc' FROM pg_proc p WHERE oid=%s::regprocedure"
    definition = "SELECT pg_get_functiondef(%s::regprocedure)"
    before = pg_conn.execute(metadata, (count,)).fetchone()
    claim_before = pg_conn.execute(definition, (claim,)).fetchone()
    count_before = pg_conn.execute(definition, (count,)).fetchone()
    _apply_version(pg_conn, "0206")
    assert pg_conn.execute(metadata, (count,)).fetchone() == before
    assert pg_conn.execute(definition, (claim,)).fetchone() == claim_before
    count_after = pg_conn.execute(definition, (count,)).fetchone()
    assert count_after != count_before
    with pytest.raises(psycopg.errors.RaiseException, match="unexpected shape"):
        _apply_version(pg_conn, "0206")
    assert pg_conn.execute(definition, (count,)).fetchone() == count_after
