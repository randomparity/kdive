"""Real-Postgres contracts for the bounded authority System retained requeue (#3530)."""

from __future__ import annotations

from typing import cast
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from kdive.db import migrate
from tests.db.authority_system_support import (
    _seed_direct_authority_case,
    _seed_direct_terminal_attempt,
)
from tests.db.external_boot_authority_support import _RoleDsns

_CAP = 4
_FINALIZE = "SELECT * FROM finalize_authority_system_attempt(%s,%s,%s,%s,%s,2,%s,%s)"


def test_0188_is_registered_once() -> None:
    assert [m.version for m in migrate.discover_migrations()].count("0188") == 1


def _retain_once(
    migrated_url: str, role_dsns: _RoleDsns, case: dict[str, object], round_number: int
) -> tuple[str, str]:
    """Seed round `round_number`'s retained receipt for the job's attempt and finalize it."""
    with psycopg.connect(migrated_url) as conn:
        if round_number > 1:
            conn.execute(
                "UPDATE jobs SET state='running',attempt=%s,worker_id=%s,"
                "lease_expires_at=clock_timestamp()+interval '5 minutes' WHERE id=%s",
                (round_number, case["worker"], case["job_id"]),
            )
        authority_id, _, receipt, head_digest, _ = _seed_direct_terminal_attempt(
            conn, case, disposition="retained-quarantine", generation=round_number
        )
        conn.commit()
    with psycopg.connect(role_dsns("kdive_worker")) as worker:
        row = worker.execute(
            _FINALIZE,
            (
                case["credential"],
                case["job_id"],
                round_number,
                authority_id,
                round_number,
                head_digest,
                receipt,
            ),
        ).fetchone()
        worker.commit()
    assert row is not None
    return row[0], row[1]


def _job_row(migrated_url: str, case: dict[str, object]) -> tuple[object, ...]:
    with psycopg.connect(migrated_url) as conn:
        row = conn.execute(
            "SELECT job.state,job.error_category,job.worker_id,job.lease_expires_at,"
            "ownership.state FROM jobs AS job, authority_system_ownership AS ownership "
            "WHERE job.id=%s AND ownership.system_id=%s",
            (case["job_id"], case["system_id"]),
        ).fetchone()
    assert row is not None
    return row


def _drive(
    migrated_url: str, role_dsns: _RoleDsns, case: dict[str, object], rounds: int
) -> list[str]:
    return [_retain_once(migrated_url, role_dsns, case, n)[1] for n in range(1, rounds + 1)]


def test_0188_persistent_provision_failure_stops_at_the_cap_and_teardown_succeeds(
    migrated_url: str, request: pytest.FixtureRequest
) -> None:
    role_dsns: _RoleDsns = request.getfixturevalue("authority_role_dsns")
    with psycopg.connect(migrated_url) as conn:
        case = _seed_direct_authority_case(
            conn, operation="provision", ownership_state="provisioning", attempt=1, max_attempts=1
        )
        conn.commit()

    assert _drive(migrated_url, role_dsns, case, _CAP) == ["queued"] * (_CAP - 1) + ["failed"]
    assert _job_row(migrated_url, case) == ("failed", "conflict", None, None, "repair-required")

    teardown_job = uuid4()
    marker = {
        **cast(dict[str, object], case["marker"]),
        "operation": "preactivation-teardown",
        "operation_identity": f"preactivation-teardown-{teardown_job}",
    }
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "INSERT INTO jobs (id,kind,state,attempt,max_attempts,payload,authorizing,dedup_key) "
            "VALUES (%s,'teardown','queued',0,3,%s,%s,%s)",
            (
                teardown_job,
                Jsonb({"authority_system_v1": marker}),
                Jsonb({"principal": "p", "agent_session": "s", "project": "proj"}),
                f"teardown-{teardown_job}",
            ),
        )
        conn.commit()
    with psycopg.connect(role_dsns("kdive_server")) as server:
        assert server.execute(
            "SELECT request_authority_system_preactivation_teardown(%s,%s,%s)",
            (case["system_id"], teardown_job, marker["operation_identity"]),
        ).fetchone() == ("applied",)
        server.commit()
    assert _job_row(migrated_url, case)[4] == "teardown-requested"


def test_0188_persistent_teardown_failure_stops_at_the_cap(
    migrated_url: str, request: pytest.FixtureRequest
) -> None:
    role_dsns: _RoleDsns = request.getfixturevalue("authority_role_dsns")
    with psycopg.connect(migrated_url) as conn:
        case = _seed_direct_authority_case(
            conn,
            operation="preactivation-teardown",
            ownership_state="teardown-requested",
            attempt=1,
            max_attempts=1,
        )
        conn.commit()

    assert _drive(migrated_url, role_dsns, case, _CAP) == ["queued"] * (_CAP - 1) + ["failed"]
    assert _job_row(migrated_url, case) == ("failed", "conflict", None, None, "teardown-requested")


def test_0188_transient_retained_receipt_still_retries(
    migrated_url: str, request: pytest.FixtureRequest
) -> None:
    role_dsns: _RoleDsns = request.getfixturevalue("authority_role_dsns")
    with psycopg.connect(migrated_url) as conn:
        case = _seed_direct_authority_case(
            conn, operation="provision", ownership_state="provisioning", attempt=1, max_attempts=1
        )
        conn.commit()

    assert _drive(migrated_url, role_dsns, case, _CAP - 1) == ["queued"] * (_CAP - 1)
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT state,attempt,max_attempts FROM jobs WHERE id=%s", (case["job_id"],)
        ).fetchone() == ("queued", _CAP - 1, _CAP)


def test_0188_a_recycled_budget_restarts_the_count(
    migrated_url: str, request: pytest.FixtureRequest
) -> None:
    role_dsns: _RoleDsns = request.getfixturevalue("authority_role_dsns")
    with psycopg.connect(migrated_url) as conn:
        case = _seed_direct_authority_case(
            conn, operation="provision", ownership_state="provisioning", attempt=1, max_attempts=1
        )
        conn.commit()
    _drive(migrated_url, role_dsns, case, _CAP - 1)
    with psycopg.connect(migrated_url) as conn:
        conn.execute("UPDATE jobs SET created_at=clock_timestamp() WHERE id=%s", (case["job_id"],))
        conn.commit()

    assert _retain_once(migrated_url, role_dsns, case, _CAP)[1] == "queued"


def test_0188_reconciler_repair_ends_an_exhausted_budget_failed(
    migrated_url: str, request: pytest.FixtureRequest
) -> None:
    role_dsns: _RoleDsns = request.getfixturevalue("authority_role_dsns")
    with psycopg.connect(migrated_url) as conn:
        case = _seed_direct_authority_case(
            conn, operation="provision", ownership_state="provisioning", attempt=1, max_attempts=1
        )
        conn.commit()
    _drive(migrated_url, role_dsns, case, _CAP - 1)
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "UPDATE jobs SET state='running',attempt=%s,worker_id=%s,"
            "lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
            (_CAP, case["worker"], case["job_id"]),
        )
        _seed_direct_terminal_attempt(
            conn, case, disposition="retained-quarantine", generation=_CAP
        )
        conn.commit()

    with psycopg.connect(role_dsns("kdive_reconciler")) as reconciler:
        assert reconciler.execute(
            "SELECT repair_terminal_authority_system_attempts(10)"
        ).fetchone() == (1,)
        reconciler.commit()
    assert _job_row(migrated_url, case) == ("failed", "conflict", None, None, "repair-required")
