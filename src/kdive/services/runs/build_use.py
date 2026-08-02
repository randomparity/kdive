"""Durable per-attempt lifetime fences for reusable build artifacts."""

from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
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
    if len(recovered_by) > 255 or len(evidence) > 1024 or len(reason) > 512:
        raise ValueError("recovery actor, evidence, or reason exceeds its storage bound")
    require_top_level_transaction(conn, "recover_build_use_after_confirmed_worker_death")
    async with conn.transaction():
        return await recover_build_use_in_transaction(
            conn,
            use_id,
            confirmed_worker_id=confirmed_worker_id,
            recovered_by=recovered_by,
            evidence=evidence,
            reason=reason,
        )


async def recover_build_use_in_transaction(
    conn: AsyncConnection,
    use_id: UUID,
    *,
    confirmed_worker_id: str,
    recovered_by: str,
    evidence: str,
    reason: str,
) -> bool:
    """Record and delete one exact use inside the caller's audit transaction."""
    if not recovered_by.strip() or not evidence.strip() or not reason.strip():
        raise ValueError("actor, independent worker-death evidence, and reason must be non-empty")
    if len(recovered_by) > 255 or len(evidence) > 1024 or len(reason) > 512:
        raise ValueError("recovery actor, evidence, or reason exceeds its storage bound")
    row = await (
        await conn.execute(
            "SELECT u.investigation_id, u.generation, u.job_id, u.attempt, "
            "u.holder_worker_id, i.project FROM investigation_build_uses u "
            "JOIN investigation_builds b ON b.investigation_id = u.investigation_id "
            "AND b.generation = u.generation "
            "JOIN investigations i ON i.id = b.investigation_id "
            "WHERE u.use_id = %s FOR UPDATE OF u, b, i",
            (use_id,),
        )
    ).fetchone()
    if row is None or row[4] != confirmed_worker_id:
        return False
    async with advisory_xact_lock(conn, LockScope.WORKER_INCARNATION, confirmed_worker_id):
        termination = await (
            await conn.execute(
                "SELECT authority_kind, authority_binding, outcome, terminated_at "
                "FROM worker_incarnations WHERE incarnation = %s AND state = 'terminated' "
                "FOR UPDATE",
                (confirmed_worker_id,),
            )
        ).fetchone()
        if termination is None:
            return False
        durable_evidence = (
            f"{termination[0]}: durable exact-incarnation termination ({termination[2]})"
        )
        await conn.execute(
            "INSERT INTO investigation_build_use_recoveries "
            "(use_id, project, investigation_id, generation, job_id, attempt, holder_worker_id, "
            "recovered_by, evidence, reason, authority_kind, authority_binding, "
            "termination_outcome, terminated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                use_id,
                row[5],
                *row[:5],
                recovered_by,
                durable_evidence,
                reason,
                termination[0],
                Jsonb(termination[1]),
                termination[2],
                termination[3],
            ),
        )
        await conn.execute(
            "DELETE FROM investigation_build_uses WHERE use_id = %s "
            "AND investigation_id = %s AND generation = %s AND job_id = %s "
            "AND attempt = %s AND holder_worker_id = %s",
            (use_id, *row[:5]),
        )
    return True
