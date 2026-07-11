"""Worker boot handler for the `runs.*` plane."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

from kdive.db.idempotency import claim_run_step, complete_run_step
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import RUNS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Run
from kdive.domain.operations.jobs import Job
from kdive.jobs.context import context_from_job as job_context_from_job
from kdive.jobs.handlers.runs import boot_evidence
from kdive.jobs.handlers.runs.common import abandon_run_step_best_effort
from kdive.jobs.payloads import RunPayload, load_payload
from kdive.jobs.provider_context import set_provider_kind
from kdive.profiles.provider_policy import ProfilePolicy
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.ports.console import ConsoleSnapshotter
from kdive.providers.ports.lifecycle import (
    Booter,
    Connector,
)
from kdive.security.authz.context import RequestContext
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import ObjectStore


async def _run_boot_and_capture_outcome(
    conn: AsyncConnection,
    job_ctx: RequestContext,
    run: Run,
    booter: Booter,
    connector: Connector,
    profile_policy: ProfilePolicy,
    secret_registry: SecretRegistry,
    artifact_store: ObjectStore | None,
    snapshotter: ConsoleSnapshotter | None,
    mark: int,
) -> dict[str, Any]:
    system_id = run.require_system_id()
    try:
        await asyncio.to_thread(booter.boot, system_id)
    except CategorizedError as exc:
        artifact = None
        if (
            exc.category is ErrorCategory.READINESS_FAILURE
            and run.expected_boot_failure is not None
        ):
            artifact = await boot_evidence.capture_run_console(
                conn, system_id, run.id, secret_registry, artifact_store, snapshotter, mark
            )
        matched_line = (
            boot_evidence.expected_crash_matched_line(run, artifact.data)
            if artifact is not None and artifact.data
            else None
        )
        if artifact is not None and matched_line is not None:
            return await boot_evidence.record_expected_crash(
                conn, job_ctx, run, system_id, profile_policy, artifact, matched_line
            )
        if exc.category is ErrorCategory.READINESS_FAILURE:
            crash = await boot_evidence.record_crash_halted_live(
                conn,
                job_ctx,
                run,
                system_id,
                connector,
                profile_policy,
                secret_registry,
                artifact_store,
                snapshotter,
                mark,
            )
            if crash is not None:
                return crash
        raise
    artifact = await boot_evidence.capture_run_console(
        conn, system_id, run.id, secret_registry, artifact_store, snapshotter, mark
    )
    await boot_evidence.record_boot_audit(conn, job_ctx, run)
    return {
        "system_id": str(system_id),
        "boot_outcome": "ready",
        **({"evidence_artifact_id": str(artifact.id)} if artifact else {}),
    }


async def boot_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
    artifact_store: ObjectStore | None = None,
) -> str | None:
    """Boot the installed kernel and confirm run-readiness, recording the `boot` step."""
    run_id = UUID(load_payload(job, RunPayload).run_id)
    run = await RUNS.get(conn, run_id)
    if run is None:
        raise CategorizedError(
            "boot target run is gone",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"run_id": str(run_id)},
        )
    job_ctx = job_context_from_job(job, run.project)
    claim = await claim_run_step(conn, run_id, "boot")
    if not claim.claimed:
        return str(run_id)
    binding = await resolver.binding_for_run(conn, run_id)
    set_provider_kind(binding.kind.value)
    booter = binding.runtime.booter
    snapshotter = None if binding.runtime.console is None else binding.runtime.console.snapshotter
    system_id = run.require_system_id()
    mark = await boot_evidence.mark_boot_window(system_id, snapshotter)

    try:
        result = await _run_boot_and_capture_outcome(
            conn,
            job_ctx,
            run,
            booter,
            binding.runtime.connector,
            binding.runtime.profile_policy,
            secret_registry,
            artifact_store,
            snapshotter,
            mark,
        )
    except CategorizedError:
        await abandon_run_step_best_effort(conn, run_id, "boot")
        try:
            await boot_evidence.capture_run_console(
                conn, system_id, run_id, secret_registry, artifact_store, snapshotter, mark
            )
        finally:
            raise
    except Exception:
        await abandon_run_step_best_effort(conn, run_id, "boot")
        raise
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.SYSTEM, system_id),
        advisory_xact_lock(conn, LockScope.RUN, run_id),
    ):
        await complete_run_step(conn, run_id, "boot", result)
    return str(run_id)
