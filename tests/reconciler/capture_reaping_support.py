"""Orphaned-capture seed helpers shared by reconciler tests (ADR-0556).

Lives outside a collected test module because ``tests/test_test_module_dependencies.py`` forbids one
test module importing another: helpers two suites share belong in a support module, so neither
suite's collection order can affect the other.

Fixtures build the job payload through the real :class:`CaptureTrafficPayload`, so a payload-shape
change reddens both suites rather than leaving a sweep that selects nothing.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from kdive.domain.capacity.state import JobState
from kdive.jobs.payloads import CaptureTrafficPayload

LOCAL = "local-libvirt"
REMOTE = "remote-libvirt"
SETTLE = timedelta(minutes=30)
PAST_SETTLE = timedelta(minutes=45)


async def connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


async def one_id(cursor: psycopg.AsyncCursor) -> UUID:
    """The single id a seeding INSERT returned."""
    row = await cursor.fetchone()
    assert row is not None
    return row[0]


async def seed_chain(
    conn: psycopg.AsyncConnection,
    *,
    kind: str = REMOTE,
    nameless: bool = False,
    domain_name: str | None = None,
) -> tuple[UUID, UUID]:
    """Insert resource -> allocation -> system -> investigation -> run; return (system, run).

    Each chain gets its own Resource name because ``resources`` is unique on ``(kind, name)``.
    ``nameless=True`` leaves the column NULL, the ownership-integrity case ADR-0187 binding
    cannot resolve a host from.
    """
    resource_id = await one_id(
        await conn.execute(
            "INSERT INTO resources (kind, name, pool, cost_class, status, host_uri) "
            "VALUES (%s, %s, 'p', 'c', 'available', 'qemu:///system') RETURNING id",
            (kind, None if nameless else f"host-{uuid4().hex[:12]}"),
        )
    )
    allocation_id = await one_id(
        await conn.execute(
            "INSERT INTO allocations (principal, project, resource_id, state) "
            "VALUES ('alice', 'proj', %s, 'active') RETURNING id",
            (resource_id,),
        )
    )
    system_id = await one_id(
        await conn.execute(
            "INSERT INTO systems (principal, project, allocation_id, state, "
            "    provisioning_profile, domain_name) "
            "VALUES ('alice', 'proj', %s, 'ready', %s, %s) RETURNING id",
            (allocation_id, Jsonb({"k": "v"}), domain_name),
        )
    )
    investigation_id = await one_id(
        await conn.execute(
            "INSERT INTO investigations (principal, project, title, state) "
            "VALUES ('alice', 'proj', 't', 'open') RETURNING id"
        )
    )
    run_id = await one_id(
        await conn.execute(
            "INSERT INTO runs (principal, project, investigation_id, system_id, target_kind, "
            "    state, build_profile) "
            "VALUES ('alice', 'proj', %s, %s, %s, 'running', %s) RETURNING id",
            (investigation_id, system_id, kind, Jsonb({"cfg": 1})),
        )
    )
    return system_id, run_id


async def seed_capture_job(
    conn: psycopg.AsyncConnection,
    run_id: UUID,
    *,
    state: JobState = JobState.FAILED,
    updated_ago: timedelta = PAST_SETTLE,
    before_cutoff: bool = True,
    attempt: int = 1,
) -> UUID:
    """Insert one ``capture_traffic`` job whose payload is the real validated model.

    Both timestamps are set in the INSERT because ``jobs_set_updated_at`` is a BEFORE UPDATE
    trigger: a later ``UPDATE ... SET updated_at`` is silently overwritten with the database
    clock, which is exactly why the sweep can treat ``jobs.updated_at`` as database-maintained.
    The migration stamps the cutover cutoff at install time, so a job is post-cutoff unless it is
    deliberately created behind that mark.
    """
    payload = CaptureTrafficPayload(run_id=str(run_id), duration_s=5, max_bytes=4096, snaplen=128)
    created = (
        "(SELECT cutoff_at - interval '1 minute' FROM capture_operation_cutoff WHERE singleton)"
        if before_cutoff
        else "now()"
    )
    cursor = await conn.execute(
        "INSERT INTO jobs (kind, payload, state, attempt, max_attempts, authorizing, dedup_key, "  # noqa: S608
        f"    created_at, updated_at) VALUES ('capture_traffic', %s, %s, %s, 5, %s, %s, {created}, "
        "    now() - %s) RETURNING id",
        (
            Jsonb(payload.model_dump(mode="json")),
            state.value,
            attempt,
            Jsonb({"principal": "p", "agent_session": None, "project": "proj"}),
            str(uuid4()),
            updated_ago,
        ),
    )
    row = await cursor.fetchone()
    assert row is not None
    return row[0]


async def reap_state(conn: psycopg.AsyncConnection, job_id: UUID) -> tuple | None:
    cursor = await conn.execute(
        "SELECT attempts, retry_after IS NOT NULL, reclaimed_at IS NOT NULL "
        "FROM capture_reap_state WHERE job_id = %s",
        (job_id,),
    )
    return await cursor.fetchone()
