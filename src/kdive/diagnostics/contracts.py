"""Dependency-neutral contracts shared by diagnostics orchestration."""

from typing import Protocol

from kdive.diagnostics.checks import CheckResult

WORKER_UNAVAILABLE_DETAIL = (
    "worker did not pick up the diagnostic job in time; check that the worker is up "
    "(/livez, /readyz) and not saturated"
)


class WorkerCheckDispatcher(Protocol):
    """Run worker-vantage checks and return their three-state results."""

    async def run_worker_checks(self) -> list[CheckResult]: ...
