"""Real-Postgres proofs that an exhausted authority job no longer wedges `running` (#2889).

ADR-0620's #2889 amendment: a public teardown recycles a teardown job whose final attempt's
lease lapsed, and migration 0162 dead-letters a non-teardown job no receipt can ever finish.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.db import migrate
from kdive.domain.operations.jobs import JobKind
from kdive.jobs import queue
from kdive.jobs.payloads import Authorizing, TeardownPayload
from kdive.reconciler.repairs.jobs import repair_abandoned_jobs
from tests.db.external_boot_authority_support import (
    _allocate,
    _AuthorityCase,
    _RoleDsns,
    _seed_case,
)
from tests.db.external_boot_journal_support import _proof
from tests.db.test_migration_0161_teardown_takeover import (
    _finalize,
    _make_current,
    _ready_teardown_case,
)

_AUTHORIZING = Authorizing(principal="p", agent_session=None, project="proj")


def test_migration_0162_is_registered() -> None:
    assert "0162" in [item.version for item in migrate.discover_migrations()]


def _lapse(migrated_url: str, job_id: UUID) -> None:
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "UPDATE jobs SET max_attempts = attempt, "
            "lease_expires_at = clock_timestamp() - interval '1 second', "
            "authorizing = authorizing || '{\"agent_session\": null}' WHERE id = %s",
            (job_id,),
        )


async def _recycle(migrated_url: str, case: _AuthorityCase) -> Any:
    with psycopg.connect(migrated_url) as conn:
        marker = conn.execute(
            "SELECT payload -> 'external_boot_authority_v1' FROM jobs WHERE id = %s",
            (case.job_id,),
        ).fetchone()
    assert marker is not None
    async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
        return await queue.enqueue(
            conn,
            JobKind.TEARDOWN,
            TeardownPayload.model_validate(
                {"system_id": str(case.system_id), "external_boot_authority_v1": marker[0]}
            ),
            _AUTHORIZING,
            f"external-authority-{case.job_id}",
            recycle=queue.JobRecyclePolicy.FAILED_OR_LAPSED_EXHAUSTED,
        )


def _heartbeat(role_dsns: _RoleDsns, case: _AuthorityCase) -> bool:
    with psycopg.connect(role_dsns("kdive_worker"), autocommit=True) as worker:
        row = worker.execute(
            "SELECT heartbeat_worker_job(%s, %s, %s, interval '5 minutes')",
            (case.job_id, case.credential, case.attempt),
        ).fetchone()
    assert row is not None
    return row[0]


def _released(migrated_url: str, case: _AuthorityCase) -> tuple[Any, ...]:
    with psycopg.connect(migrated_url) as conn:
        row = conn.execute(
            "SELECT (SELECT count(*) FROM external_boot_reservation_releases "
            "        WHERE activation_id = %s), "
            "       (SELECT state FROM jobs WHERE id = %s)",
            (case.activation_id, case.job_id),
        ).fetchone()
    assert row is not None
    return row


@pytest.mark.anyio
async def test_recycled_lapsed_final_attempt_is_fenced_and_credits_once(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case = _ready_teardown_case(migrated_url, "x")
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        old = _allocate(worker, case)
    proof = _proof(case, "complete_ready")
    with psycopg.connect(migrated_url) as conn:
        old_digest = _make_current(conn, case, old, proof, 2)
    _lapse(migrated_url, case.job_id)

    recycled = await _recycle(migrated_url, case)

    assert (recycled.id, recycled.state.value) == (case.job_id, "queued")
    assert (recycled.attempt, recycled.max_attempts) == (1, 2)
    # The dead attempt can neither renew its lease nor finalize with its still-current authority.
    assert not _heartbeat(authority_role_dsns, case)
    assert _finalize(authority_role_dsns, case, old, proof, 2, old_digest) == "superseded"

    # A different incarnation claims the recycled job, as claim_worker_job would.
    successor = replace(
        case,
        attempt=2,
        worker_id=f"docker:external-authority-x2-{uuid4()}",
        credential=b"y" * 32,
    )
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "INSERT INTO worker_incarnations "
            "(incarnation, authority_kind, authority_binding, credential_hash, fence_protocol) "
            "VALUES (%s, 'docker', '{}'::jsonb, %s, 4)",
            (successor.worker_id, successor.credential),
        )
        conn.execute(
            "UPDATE jobs SET state = 'running', attempt = 2, worker_id = %s, "
            "lease_expires_at = now() + interval '5 minutes', heartbeat_at = now() WHERE id = %s",
            (successor.worker_id, case.job_id),
        )
    assert not _heartbeat(authority_role_dsns, case)
    assert _finalize(authority_role_dsns, case, old, proof, 2, old_digest) == "superseded"
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        new = _allocate(worker, successor)
    with psycopg.connect(migrated_url) as conn:
        new_digest = _make_current(conn, successor, new, proof, 4)
    assert _finalize(authority_role_dsns, successor, new, proof, 4, new_digest) == "applied"
    assert _finalize(authority_role_dsns, successor, new, proof, 4, new_digest) == "applied"
    assert _released(migrated_url, case) == (1, "succeeded")

    # A teardown retried after success replays the succeeded job and credits nothing more.
    replayed = await _recycle(migrated_url, case)
    assert (replayed.id, replayed.state.value) == (case.job_id, "succeeded")
    assert _released(migrated_url, case) == (1, "succeeded")


def _stray(migrated_url: str, suffix: str, *, purpose: str = "activate") -> _AuthorityCase:
    with psycopg.connect(migrated_url) as seed:
        return _seed_case(seed, purpose=purpose, worker_suffix=suffix)


def _lapse_with_running_run(migrated_url: str, case: _AuthorityCase) -> None:
    """Lapse the final attempt; a `running` Run shows whether the dead-letter compensates it."""
    _lapse(migrated_url, case.job_id)
    with psycopg.connect(migrated_url) as conn:
        conn.execute("UPDATE runs SET state = 'running' WHERE id = %s", (case.run_id,))


def _dead_letter(role_dsns: _RoleDsns, role: str = "kdive_reconciler") -> list[UUID]:
    with psycopg.connect(role_dsns(role), autocommit=True) as conn:
        rows = conn.execute("SELECT * FROM dead_letter_unowned_external_boot_jobs()").fetchall()
    return [row[0] for row in rows]


def _job_and_run(migrated_url: str, case: _AuthorityCase) -> tuple[Any, ...]:
    with psycopg.connect(migrated_url) as conn:
        row = conn.execute(
            "SELECT j.state, j.error_category, r.state FROM jobs j, runs r "
            "WHERE j.id = %s AND r.id = %s",
            (case.job_id, case.run_id),
        ).fetchone()
    assert row is not None
    return row


def _set_authority_state(migrated_url: str, authority_id: UUID, state: str) -> None:
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "UPDATE external_boot_authorities SET state = %s, "
            "superseded_at = CASE WHEN %s = 'superseded' THEN now() END, "
            "acknowledged_at = CASE WHEN %s = 'current' THEN now() END WHERE id = %s",
            (state, state, state, authority_id),
        )


def test_unowned_exhausted_stray_job_is_dead_lettered(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    never_allocated = _stray(migrated_url, "n")
    superseded = _stray(migrated_url, "s")
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, superseded)
    _set_authority_state(migrated_url, authority.authority_id, "superseded")
    for case in (never_allocated, superseded):
        _lapse_with_running_run(migrated_url, case)

    dead = _dead_letter(authority_role_dsns)

    assert set(dead) == {never_allocated.job_id, superseded.job_id}
    for case in (never_allocated, superseded):
        assert _job_and_run(migrated_url, case) == ("failed", "lease_expired", "failed")
    assert _dead_letter(authority_role_dsns) == []


@pytest.mark.parametrize("state", ["allocating", "current"])
def test_stray_job_with_a_live_authority_is_kept(
    migrated_url: str, authority_role_dsns: _RoleDsns, state: str
) -> None:
    case = _stray(migrated_url, "l")
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)
    _set_authority_state(migrated_url, authority.authority_id, state)
    _lapse_with_running_run(migrated_url, case)

    assert case.job_id not in _dead_letter(authority_role_dsns)
    assert _job_and_run(migrated_url, case) == ("running", None, "running")


@pytest.mark.parametrize("shape", ["teardown", "live-lease", "not-exhausted", "unmarked"])
def test_dead_letter_keeps_jobs_outside_its_set(
    migrated_url: str, authority_role_dsns: _RoleDsns, shape: str
) -> None:
    case = _stray(migrated_url, "o", purpose="teardown" if shape == "teardown" else "activate")
    with psycopg.connect(migrated_url) as conn:
        if shape == "live-lease":
            conn.execute("UPDATE jobs SET max_attempts = attempt WHERE id = %s", (case.job_id,))
        elif shape == "not-exhausted":
            conn.execute(
                "UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
                (case.job_id,),
            )
        else:
            _lapse(migrated_url, case.job_id)
            if shape == "unmarked":
                conn.execute(
                    "UPDATE jobs SET payload = payload - 'external_boot_authority_v1' "
                    "WHERE id = %s",
                    (case.job_id,),
                )

    assert case.job_id not in _dead_letter(authority_role_dsns)
    assert _job_and_run(migrated_url, case)[:2] == ("running", None)


@pytest.mark.parametrize("role", ["kdive_server", "kdive_worker"])
def test_only_the_reconciler_may_dead_letter(authority_role_dsns: _RoleDsns, role: str) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _dead_letter(authority_role_dsns, role)


@pytest.mark.anyio
async def test_repair_abandoned_jobs_dead_letters_the_unowned_stray(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case = _stray(migrated_url, "r")
    _lapse_with_running_run(migrated_url, case)

    async with await psycopg.AsyncConnection.connect(
        authority_role_dsns("kdive_reconciler")
    ) as conn:
        swept = await repair_abandoned_jobs(conn)

    assert swept == 1
    assert _job_and_run(migrated_url, case) == ("failed", "lease_expired", "failed")
