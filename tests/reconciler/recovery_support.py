"""Distinct persisted recovery premises shared by reconciler proofs."""

from __future__ import annotations

from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from kdive.domain.capacity.state import JobState
from kdive.services.systems.authority_owned import _teardown_dedup_key
from tests.reconciler.conftest import seed_run


async def seed_teardown_job(
    conn: psycopg.AsyncConnection,
    system_id: UUID,
    *,
    state: JobState,
    attempt: int = 3,
) -> None:
    await conn.execute(
        "INSERT INTO jobs (kind, payload, state, attempt, max_attempts, authorizing, dedup_key, "
        "error_category, failure_context) "
        "VALUES ('teardown', %s, %s, %s, 3, %s, %s, 'control_failure', %s)",
        (
            Jsonb({"system_id": str(system_id)}),
            state.value,
            attempt,
            Jsonb({"principal": "reconciler-test", "agent_session": None, "project": "proj"}),
            f"{system_id}:teardown",
            Jsonb({"teardown_recovery_reason": "teardown_automatic_replay_available"}),
        ),
    )


async def teardown_job(conn: psycopg.AsyncConnection, system_id: UUID) -> tuple[str, int] | None:
    row = await (
        await conn.execute(
            "SELECT state, attempt FROM jobs WHERE dedup_key = %s", (f"{system_id}:teardown",)
        )
    ).fetchone()
    return None if row is None else (str(row[0]), int(row[1]))


async def seed_completed_activation(conn: psycopg.AsyncConnection, system_id: UUID) -> UUID:
    """Cleanup-complete abandoned history without a settlement record; it still restricts."""
    run_id = await seed_run(conn, system_id)
    activation_id = uuid4()
    ids = {"activation_id": str(activation_id), "system_id": str(system_id)}
    when = "2026-09-01T00:00:00+00:00"
    digest = "sha256:" + "b" * 64
    await conn.execute(
        "INSERT INTO external_boot_activations "
        "(id, system_id, run_id, plan_identity, operation_owner_id, authority_generation, "
        "state, cleanup_complete, terminal_evidence, cleanup_evidence) "
        "VALUES (%s, %s, %s, %s, %s, 1, 'abandoned', true, %s, %s)",
        (
            activation_id,
            system_id,
            run_id,
            "sha256:" + "a" * 64,
            uuid4(),
            Jsonb(
                {
                    "schema": "external-boot-terminal-evidence-v1",
                    "outcome": "abandoned",
                    "composite_state": digest,
                    "objects": [],
                    "observed_at": when,
                    **ids,
                }
            ),
            Jsonb(
                {
                    "schema": "external-boot-cleanup-evidence-v1",
                    "mode": "ordinary",
                    "release_identity": digest,
                    "completed_at": when,
                    **ids,
                }
            ),
        ),
    )
    return activation_id


async def seed_aged_teardown_job(
    conn: psycopg.AsyncConnection, system_id: UUID, *, state: str, age_seconds: int = 0
) -> None:
    """A teardown job at the dedup key both teardown families use, in ``state``.

    Keyed through the production `_teardown_dedup_key`, which the lane rebuilds by hand in Python
    and again in SQL: a change to that convention then fails the deferral arms below rather than
    silently stopping the lane from deferring.

    ``age_seconds`` backdates ``updated_at`` so an arm can put a terminal job outside the settle
    window. It goes in the INSERT because `jobs_set_updated_at` is a BEFORE UPDATE trigger that
    reassigns the column unconditionally, so a later UPDATE that backdates it is overwritten.
    """
    await conn.execute(
        "INSERT INTO jobs (kind, payload, state, attempt, max_attempts, authorizing, dedup_key, "
        "    updated_at) "
        "VALUES ('teardown', %s, %s, 1, 3, %s, %s, now() - make_interval(secs => %s))",
        (
            Jsonb({"system_id": str(system_id)}),
            state,
            Jsonb({"principal": "alice", "project": "proj"}),
            _teardown_dedup_key(system_id),
            age_seconds,
        ),
    )


async def seed_reprovision_job(
    conn: psycopg.AsyncConnection,
    system_id: UUID,
    *,
    state: str,
    error_category: str | None = None,
    attempt: int = 1,
    age_seconds: int = 0,
) -> None:
    """A reprovision job for ``system_id``, last written ``age_seconds`` ago.

    ``updated_at`` is backdated in the INSERT because `jobs_set_updated_at` is a BEFORE UPDATE
    trigger that overwrites the column on any later UPDATE.
    """
    await conn.execute(
        "INSERT INTO jobs (kind, payload, state, attempt, max_attempts, authorizing, dedup_key, "
        "    error_category, updated_at) "
        "VALUES ('reprovision', %s, %s, %s, 3, %s, %s, %s, now() - make_interval(secs => %s))",
        (
            Jsonb({"system_id": str(system_id), "profile_digest": "d"}),
            state,
            attempt,
            Jsonb({"principal": "alice", "agent_session": None, "project": "proj"}),
            f"{system_id}:reprovision:{uuid4()}",
            error_category,
            age_seconds,
        ),
    )
