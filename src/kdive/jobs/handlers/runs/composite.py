"""Composite build->install->boot worker handler (ADR-0268, #866).

One job runs the three phases in sequence by calling the existing per-phase executors. Each
executor commits its own `run_steps` row; the first phase that raises stops the sequence and the
error propagates to the worker (which marks the job failed), tagged with `failed_phase`.
"""

from __future__ import annotations

from psycopg import AsyncConnection

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job
from kdive.jobs.handlers.runs.boot import boot_handler
from kdive.jobs.handlers.runs.build import build_handler
from kdive.jobs.handlers.runs.install import install_handler
from kdive.jobs.handlers.runs.ports import RunHandlerPorts
from kdive.jobs.payloads import BuildInstallBootPayload, BuildPayload, RunPayload


class CompositePhaseError(CategorizedError):
    """A composite phase failed; `failed_phase` names which (build|install|boot).

    Extends `CategorizedError` so the worker's `_failure_context` picks up `failed_phase`
    from `.details` and persists it as `failure_detail_failed_phase` on the job row.
    """

    def __init__(self, failed_phase: str, cause: BaseException) -> None:
        category = (
            cause.category
            if isinstance(cause, CategorizedError)
            else ErrorCategory.INFRASTRUCTURE_FAILURE
        )
        super().__init__(
            f"{failed_phase} phase failed: {cause}",
            category=category,
            details={"failed_phase": failed_phase},
        )
        self.failed_phase = failed_phase
        self.__cause__ = cause


def _phase_job(job: Job, payload: BuildPayload | RunPayload) -> Job:
    """A copy of `job` carrying a phase-specific payload.

    Per-phase executors use `extra="forbid"` payloads, so the composite's own
    `BuildInstallBootPayload` cannot be passed through; each phase gets the exact
    payload shape it expects. `Job` is a Pydantic `DomainModel`, so copy with
    `model_copy(update=...)` and serialize the phase payload to a dict.
    """
    return job.model_copy(update={"payload": payload.model_dump(mode="json")})


async def composite_handler(
    conn: AsyncConnection, job: Job, *, ports: RunHandlerPorts
) -> str | None:
    """Run build -> install -> boot in sequence, short-circuiting on the first failure."""
    base = BuildInstallBootPayload.model_validate(job.payload)
    run_id = base.run_id

    build_job = _phase_job(
        job, BuildPayload(run_id=run_id, cmdline=base.cmdline, build_host_id=base.build_host_id)
    )
    run_only = _phase_job(job, RunPayload(run_id=run_id))

    try:
        await build_handler(
            conn,
            build_job,
            resolver=ports.resolver,
            secret_registry=ports.secret_registry,
            transport_factories=ports.transport_factories,
            build_phase_recorder=ports.build_phase_recorder,
        )
    except Exception as exc:  # noqa: BLE001 - re-tagged with the failed phase, then re-raised
        raise CompositePhaseError("build", exc) from exc
    try:
        await install_handler(conn, run_only, resolver=ports.resolver)
    except Exception as exc:  # noqa: BLE001
        raise CompositePhaseError("install", exc) from exc
    try:
        await boot_handler(
            conn,
            run_only,
            resolver=ports.resolver,
            secret_registry=ports.secret_registry,
            artifact_store=ports.artifact_store,
        )
    except Exception as exc:  # noqa: BLE001
        raise CompositePhaseError("boot", exc) from exc
    return None
