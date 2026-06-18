"""Provider-owned diagnostic contributions consumed by the default diagnostics service."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from kdive.diagnostics.checks import Check


@dataclass(frozen=True, slots=True)
class WorkerVantageDescriptor:
    """A provider worker-vantage check that is declared but not runnable in this process."""

    id: str
    provider: str


@dataclass(frozen=True, slots=True)
class DiagnosticProviderContribution:
    """Provider-owned diagnostics assembly hooks."""

    provider: str
    enabled: Callable[[], bool]
    checks: Callable[[], Sequence[Check]]
    unavailable_worker_checks: Callable[[], Sequence[WorkerVantageDescriptor]]
    worker_checks: Callable[[], Sequence[Check]]
