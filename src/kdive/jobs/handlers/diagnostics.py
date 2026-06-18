"""Worker handler for provider-owned diagnostics_worker_check jobs (ADR-0164)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from psycopg import AsyncConnection

from kdive.diagnostics.checks import Check, run_check
from kdive.diagnostics.result_codec import serialize_results
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import DiagnosticsWorkerCheckPayload, load_payload

_PER_CHECK_TIMEOUT_S = 6.0

WorkerCheckBuilder = Callable[[], Sequence[Check]]
WorkerCheckBuilders = Mapping[str, WorkerCheckBuilder]


def _worker_check_builders() -> dict[str, WorkerCheckBuilder]:
    from kdive.providers.assembly.diagnostics import diagnostic_provider_contributions

    return {
        contribution.provider: contribution.worker_checks
        for contribution in diagnostic_provider_contributions()
        if contribution.enabled()
    }


async def diagnostics_worker_check_handler(
    conn: AsyncConnection | None,
    job: Job | None,
    *,
    worker_check_builders: WorkerCheckBuilders | None = None,
) -> str | None:
    """Run the worker-vantage checks and return their results inline as result_ref.

    Provider check-construction failures propagate so the job dead-letters; ``conn`` is unused
    because the provider contribution resolves any required host config through its own boundary.
    """
    del conn
    if job is None:
        raise CategorizedError(
            "diagnostics worker check handler requires a job payload",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    payload = load_payload(job, DiagnosticsWorkerCheckPayload)
    builders = worker_check_builders or _worker_check_builders()
    builder = builders.get(payload.provider)
    if builder is None:
        raise CategorizedError(
            "no diagnostics worker checks are registered for provider",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"provider": payload.provider},
        )
    checks = builder()
    results = [await run_check(check, timeout=_PER_CHECK_TIMEOUT_S) for check in checks]
    return serialize_results(results)


def register_handlers(registry: HandlerRegistry) -> None:
    """Bind the diagnostics_worker_check job handler."""
    registry.register(
        JobKind.DIAGNOSTICS_WORKER_CHECK,
        lambda conn, job: diagnostics_worker_check_handler(conn, job),
    )
