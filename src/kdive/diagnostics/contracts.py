"""Dependency-neutral contracts shared by diagnostics orchestration."""

WORKER_UNAVAILABLE_DETAIL = (
    "worker did not pick up the diagnostic job in time; check that the worker is up "
    "(/livez, /readyz) and not saturated"
)
