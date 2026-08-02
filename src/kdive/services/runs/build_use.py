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
    run = await RUNS.get(conn, run_id)
    if run is None or run.build_ref is None:
        return None
    use_id = uuid4()
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.INVESTIGATION, run.investigation_id),
    ):
        build = await resolve_build(conn, run.investigation_id, run.build_ref)
        if build is None or build.state != "active":
            raise RuntimeError("reusable build became unavailable before install execution")
        await conn.execute(
            "INSERT INTO investigation_build_uses "
            "(use_id, investigation_id, generation, job_id, attempt) "
            "VALUES (%s, %s, %s, %s, %s)",
            (use_id, run.investigation_id, build.generation, job_id, attempt),
        )
    return use_id


async def release_build_use(conn: AsyncConnection, use_id: UUID | None) -> None:
    """Release only this executing attempt's fence; a failure remains a safe pin."""
    if use_id is None:
        return
    require_top_level_transaction(conn, "release_build_use")
    async with conn.transaction():
        await conn.execute("DELETE FROM investigation_build_uses WHERE use_id = %s", (use_id,))
