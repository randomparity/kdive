"""Durable per-attempt lifetime fences for reusable build artifacts."""

from uuid import UUID, uuid4

from psycopg import AsyncConnection
from pydantic import SecretStr

from kdive.db.locks import LockScope, advisory_xact_lock, require_top_level_transaction
from kdive.db.repositories import RUNS
from kdive.services.runs.build_catalog import resolve_build


async def acquire_build_use(
    conn: AsyncConnection,
    run_id: UUID,
    *,
    job_id: UUID,
    attempt: int,
    incarnation_credential: SecretStr,
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
        acquired = await (
            await conn.execute(
                "SELECT public.acquire_investigation_build_use(%s, %s, %s, %s, %s, "
                "sha256(convert_to(%s, 'UTF8')))",
                (
                    use_id,
                    run.investigation_id,
                    build.generation,
                    job_id,
                    attempt,
                    incarnation_credential.get_secret_value(),
                ),
            )
        ).fetchone()
        assert acquired is not None
        if not acquired[0]:
            raise RuntimeError("install job has no credential-owned executing claim")
    return use_id


async def release_build_use(
    conn: AsyncConnection,
    use_id: UUID | None,
    *,
    incarnation_credential: SecretStr,
) -> bool:
    """Release only this executing attempt's fence; a failure remains a safe pin."""
    if use_id is None:
        return True
    require_top_level_transaction(conn, "release_build_use")
    async with conn.transaction():
        row = await (
            await conn.execute(
                "SELECT public.release_investigation_build_use(%s, sha256(convert_to(%s, 'UTF8')))",
                (use_id, incarnation_credential.get_secret_value()),
            )
        ).fetchone()
    assert row is not None
    return bool(row[0])


async def recover_build_use_after_confirmed_worker_death(
    conn: AsyncConnection,
    use_id: UUID,
    *,
    authorized_projects: tuple[str, ...],
    confirmed_worker_id: str,
    recovered_by: str,
    evidence: str,
    reason: str,
) -> bool:
    """Release a stranded use only after external proof that its worker process died.

    Time and the job lease are deliberately not evidence: ADR-0018 permits the old handler to
    continue after heartbeat loss and job reclaim. This explicit operator/reconciler path records
    the independently obtained evidence before removing the persistent pin.
    """
    if not recovered_by.strip() or not evidence.strip() or not reason.strip():
        raise ValueError("actor, independent worker-death evidence, and reason must be non-empty")
    if (
        len(recovered_by.encode()) > 255
        or len(evidence.encode()) > 1024
        or len(reason.encode()) > 512
    ):
        raise ValueError("recovery actor, evidence, or reason exceeds its storage bound")
    require_top_level_transaction(conn, "recover_build_use_after_confirmed_worker_death")
    async with conn.transaction():
        return await recover_build_use_in_transaction(
            conn,
            use_id,
            authorized_projects=authorized_projects,
            confirmed_worker_id=confirmed_worker_id,
            recovered_by=recovered_by,
            evidence=evidence,
            reason=reason,
        )


async def recover_build_use_in_transaction(
    conn: AsyncConnection,
    use_id: UUID,
    *,
    authorized_projects: tuple[str, ...],
    confirmed_worker_id: str,
    recovered_by: str,
    evidence: str,
    reason: str,
) -> bool:
    """Invoke the bounded evidence-checked recovery inside the caller's audit transaction."""
    if not recovered_by.strip() or not evidence.strip() or not reason.strip():
        raise ValueError("actor, independent worker-death evidence, and reason must be non-empty")
    if (
        len(recovered_by.encode()) > 255
        or len(evidence.encode()) > 1024
        or len(reason.encode()) > 512
    ):
        raise ValueError("recovery actor, evidence, or reason exceeds its storage bound")
    row = await (
        await conn.execute(
            "SELECT public.recover_investigation_build_use(%s, %s::text[], %s, %s, %s)",
            (use_id, list(authorized_projects), confirmed_worker_id, recovered_by, reason),
        )
    ).fetchone()
    assert row is not None
    return bool(row[0])
