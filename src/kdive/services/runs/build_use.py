"""Durable per-attempt lifetime fences for reusable build artifacts."""

from uuid import UUID, uuid4

from psycopg import AsyncConnection

from kdive.db.locks import LockScope, advisory_xact_lock, require_top_level_transaction
from kdive.db.repositories import RUNS
from kdive.services.runs.build_catalog import resolve_build


async def acquire_build_use(
    conn: AsyncConnection, run_id: UUID, *, job_id: UUID, attempt: int
) -> UUID | None:
    """Fence a reusable generation for this exact executing job attempt."""
    require_top_level_transaction(conn, "acquire_build_use")
    use_id = uuid4()
    async with conn.transaction():
        run = await RUNS.get(conn, run_id)
        if run is None or run.build_ref is None:
            return None
        async with advisory_xact_lock(conn, LockScope.INVESTIGATION, run.investigation_id):
            locked_run = await RUNS.get(conn, run_id)
            if locked_run is None or locked_run.build_ref != run.build_ref:
                raise RuntimeError("reusable build selection changed before install execution")
        build = await resolve_build(conn, run.investigation_id, run.build_ref)
        if build is None or build.state != "active":
            raise RuntimeError("reusable build became unavailable before install execution")
        claim = await (
            await conn.execute(
                "SELECT worker_id, lease_expires_at FROM jobs WHERE id = %s "
                "AND state = 'running' AND attempt = %s FOR UPDATE",
                (job_id, attempt),
            )
        ).fetchone()
        if claim is None or claim[0] is None or claim[1] is None:
            raise RuntimeError("install job no longer has a live executing claim")
        await conn.execute(
            "INSERT INTO investigation_build_uses "
            "(use_id, investigation_id, generation, job_id, attempt, holder_worker_id, "
            "lease_expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                use_id,
                run.investigation_id,
                build.generation,
                job_id,
                attempt,
                claim[0],
                claim[1],
            ),
        )
    return use_id


async def release_build_use(conn: AsyncConnection, use_id: UUID | None) -> None:
    """Release only this executing attempt's fence; a failure remains a safe pin."""
    if use_id is None:
        return
    require_top_level_transaction(conn, "release_build_use")
    async with conn.transaction():
        await conn.execute("DELETE FROM investigation_build_uses WHERE use_id = %s", (use_id,))


async def recover_build_use_after_confirmed_worker_death(
    conn: AsyncConnection,
    use_id: UUID,
    *,
    confirmed_worker_id: str,
    recovered_by: str,
    evidence: str,
) -> bool:
    """Release a stranded use only after external proof that its worker process died.

    Time and the job lease are deliberately not evidence: ADR-0018 permits the old handler to
    continue after heartbeat loss and job reclaim. This explicit operator/reconciler path records
    the independently obtained evidence before removing the persistent pin.
    """
    if not recovered_by.strip() or not evidence.strip():
        raise ValueError("recovered_by and independent worker-death evidence must be non-empty")
    require_top_level_transaction(conn, "recover_build_use_after_confirmed_worker_death")
    async with conn.transaction():
        row = await (
            await conn.execute(
                "SELECT investigation_id, generation, job_id, attempt, holder_worker_id "
                "FROM investigation_build_uses WHERE use_id = %s FOR UPDATE",
                (use_id,),
            )
        ).fetchone()
        if row is None or row[4] != confirmed_worker_id:
            return False
        await conn.execute(
            "INSERT INTO investigation_build_use_recoveries "
            "(use_id, investigation_id, generation, job_id, attempt, holder_worker_id, "
            "recovered_by, evidence) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (use_id, *row, recovered_by, evidence),
        )
        await conn.execute("DELETE FROM investigation_build_uses WHERE use_id = %s", (use_id,))
    return True
