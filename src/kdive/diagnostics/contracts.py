"""Dependency-neutral contracts shared by diagnostics orchestration."""

from typing import Protocol

from kdive.diagnostics.checks import CheckResult

WORKER_UNAVAILABLE_DETAIL = (
    "worker did not pick up the diagnostic job in time; check that the worker is up "
    "(/livez, /readyz) and not saturated"
)


class WorkerCheckDispatcher(Protocol):
    """Run worker-vantage checks and return their three-state results."""

    async def run_worker_checks(self, *, deadline: float | None = None) -> list[CheckResult]:
        """Use the shared report deadline in event-loop monotonic seconds, when supplied.

        Expiry returns attributed errors for unfinished checks; callers can retry diagnostics
        after worker readiness is restored. None retains the dispatcher's local budget.
        """
        ...
