"""The aggregating diagnostics service (ADR-0091 §1, §2).

`DiagnosticsService` runs an assembled set of checks — each bounded by the per-check
timeout via :func:`kdive.diagnostics.checks.run_check` — and aggregates them into one
:class:`DiagnosticsReport`. Aggregation keeps the three-state distinction: ``has_failure``
counts only contract violations, and an ``error`` (a check that could not run) never
inflates into a failure.

`doctor` diagnoses a deployment whose **core is up**; it does not replace the health
endpoints (ADR-0090). The worker-vantage checks run as worker jobs, so the service needs
the worker reachable just to *run* them. When the worker is unavailable, those checks
surface as ``error`` results pointing at the health endpoints — **not** a hang, and not a
contract ``fail`` (the tool that explains breakage must not wedge on it).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import kdive.config as config
from kdive.config.core_settings import SECRETS_ROOT
from kdive.diagnostics.checks import (
    Check,
    CheckResult,
    CheckStatus,
    Vantage,
    run_check,
)
from kdive.diagnostics.provider_contracts import (
    DiagnosticProviderContribution,
    WorkerVantageDescriptor,
)
from kdive.diagnostics.secret_ref import SecretRefCheck
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.security.secrets.paths import PathSafetyError
from kdive.security.secrets.secrets import read_secret_file

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

    # Runtime-import only inside default_service_factory to avoid a cycle: worker_dispatch imports
    # WORKER_UNAVAILABLE_DETAIL from this module (ADR-0164).
    from kdive.diagnostics.worker_dispatch import WorkerCheckDispatcher

WORKER_UNAVAILABLE_DETAIL = (
    "worker did not pick up the diagnostic job in time; check that the worker is up "
    "(/livez, /readyz) and not saturated"
)
FEATURE_NOT_ENABLED_DETAIL = (
    "worker-vantage diagnostic checks (provider_tls, gdbstub_acl) are not enabled "
    "in this deployment"
)

_TRANSPORT_FAILURE = ErrorCategory.TRANSPORT_FAILURE
_NOT_IMPLEMENTED = ErrorCategory.NOT_IMPLEMENTED

_DEFAULT_PER_CHECK_TIMEOUT = 10.0
_DEFAULT_OVERALL_TIMEOUT = 30.0


class WorkerVantageSubstitution(StrEnum):
    """Why a worker-vantage check is substituted instead of run (ADR-0139).

    The two causes carry **distinct** details and ``failure_category`` labels so an operator —
    and a programmatic caller — can tell them apart without parsing prose:

    - ``WORKER_UNAVAILABLE`` — dispatch exists but the worker cannot pick the job up; the detail
      points at the health endpoints (ADR-0090). This is the default substitution cause.
    - ``FEATURE_NOT_ENABLED`` — no worker-job dispatch is wired in this deployment, so the check
      cannot run regardless of worker health (#484). Pointing at ``/livez``/``/readyz`` here is
      misleading (it reads as a worker outage); the detail says the feature is not enabled.
    """

    WORKER_UNAVAILABLE = "worker_unavailable"
    FEATURE_NOT_ENABLED = "feature_not_enabled"


_SUBSTITUTION_DETAIL: dict[WorkerVantageSubstitution, str] = {
    WorkerVantageSubstitution.WORKER_UNAVAILABLE: WORKER_UNAVAILABLE_DETAIL,
    WorkerVantageSubstitution.FEATURE_NOT_ENABLED: FEATURE_NOT_ENABLED_DETAIL,
}
_SUBSTITUTION_CATEGORY: dict[WorkerVantageSubstitution, ErrorCategory] = {
    WorkerVantageSubstitution.WORKER_UNAVAILABLE: _TRANSPORT_FAILURE,
    WorkerVantageSubstitution.FEATURE_NOT_ENABLED: _NOT_IMPLEMENTED,
}


class _SecretBackendUnreachable(Exception):
    """The secret backend root is absent — a check-cannot-run condition, not a per-ref miss."""


@dataclass(frozen=True, slots=True)
class WorkerVantageCheck:
    """A worker-vantage diagnostic that is reported as unavailable, not run."""

    id: str
    provider: str | None = None


@dataclass(frozen=True, slots=True)
class WorkerVantageSubstitutionMode:
    """Report worker-vantage checks as substituted errors instead of running them."""

    reason: WorkerVantageSubstitution
    checks: Sequence[WorkerVantageCheck] = ()


@dataclass(frozen=True, slots=True)
class WorkerVantageDispatchMode:
    """Delegate all worker-vantage outcomes to a dispatcher."""

    dispatcher: WorkerCheckDispatcher


@dataclass(frozen=True, slots=True)
class _CompositeWorkerCheckDispatcher:
    """Run several provider dispatchers as one worker-vantage dispatcher."""

    dispatchers: Sequence[WorkerCheckDispatcher]

    async def run_worker_checks(self) -> list[CheckResult]:
        results: list[CheckResult] = []
        for dispatcher in self.dispatchers:
            results.extend(await dispatcher.run_worker_checks())
        return results


type WorkerVantageMode = WorkerVantageSubstitutionMode | WorkerVantageDispatchMode


def worker_unavailable_results(
    checks: Sequence[Check | WorkerVantageCheck],
    reason: WorkerVantageSubstitution = WorkerVantageSubstitution.WORKER_UNAVAILABLE,
) -> list[CheckResult]:
    """Return an ``error`` result per worker-vantage check that is substituted, not run.

    The detail and ``failure_category`` attribute the substitution cause (ADR-0139): a genuine
    worker outage points at the health endpoints (ADR-0090); an unwired feature says so instead
    of misdirecting triage to ``/livez``/``/readyz``. Either way it is an ``error``, never a
    contract ``fail`` (no fix string) — the diagnostic that explains breakage must not wedge on
    the breakage it exists to explain (ADR-0091 §1).
    """
    return [
        CheckResult(
            check_id=check.id,
            status=CheckStatus.ERROR,
            detail=_SUBSTITUTION_DETAIL[reason],
            provider=check.provider if isinstance(check, WorkerVantageCheck) else None,
            failure_category=_SUBSTITUTION_CATEGORY[reason],
        )
        for check in checks
    ]


@dataclass(frozen=True, slots=True)
class DiagnosticsReport:
    """One coherent verdict over every run check (ADR-0091 §2)."""

    results: list[CheckResult]

    @property
    def has_failure(self) -> bool:
        """Whether any check reported a contract ``fail`` (a gate must exit nonzero)."""
        return any(r.status is CheckStatus.FAIL for r in self.results)

    @property
    def has_error(self) -> bool:
        """Whether any check could not be run to a verdict (reported distinctly)."""
        return any(r.status is CheckStatus.ERROR for r in self.results)


class DiagnosticsService:
    """Runs the assembled checks and aggregates them into one report."""

    def __init__(
        self,
        *,
        checks: Sequence[Check],
        per_check_timeout: float,
        overall_timeout: float | None = None,
        worker_mode: WorkerVantageMode | None = None,
    ) -> None:
        """Build the service.

        Args:
            checks: The assembled checks to run (server- and worker-vantage).
            per_check_timeout: The per-check timeout bound; a check that does not answer
                within it is ``error`` (never a hang).
            overall_timeout: The deadline across the whole run (ADR-0091 §2). Once it is
                exhausted, every not-yet-run check is reported ``error`` instead of being
                run, so used as a gate ``doctor`` reports a clean ``error`` rather than
                hanging on a black-holed host. ``None`` bounds the run only per check.
            worker_mode: How worker-vantage checks are handled. ``None`` runs worker-vantage
                ``Check`` objects directly when they are in ``checks``. Substitution mode reports
                skipped worker checks and declared unavailable checks as explicit errors with a
                named cause. Dispatch mode delegates the worker-vantage outcome to the worker-job
                dispatcher (ADR-0164).
        """
        self._checks = list(checks)
        self._timeout = per_check_timeout
        self._overall_timeout = overall_timeout
        self._worker_mode = worker_mode

    async def run(self) -> DiagnosticsReport:
        """Run every check and return the aggregated report.

        Checks run sequentially, each bounded by the per-check timeout and the remaining
        overall budget (the smaller of the two). When the overall deadline is exhausted,
        the not-yet-run checks are reported ``error`` rather than run — a gate sees a clean
        verdict instead of a hang.
        """
        runnable = [c for c in self._checks if self._can_run(c)]
        skipped = [c for c in self._checks if not self._can_run(c)]
        results = await self._run_within_budget(runnable)
        if isinstance(self._worker_mode, WorkerVantageDispatchMode):
            # The dispatcher owns the entire worker-vantage outcome (run on the worker, or a
            # substituted error on a worker that does not pick the job up in time) — ADR-0164.
            results.extend(await self._worker_mode.dispatcher.run_worker_checks())
        elif isinstance(self._worker_mode, WorkerVantageSubstitutionMode):
            results.extend(worker_unavailable_results(skipped, self._worker_mode.reason))
            results.extend(
                worker_unavailable_results(
                    self._worker_mode.checks,
                    self._worker_mode.reason,
                )
            )
        return DiagnosticsReport(results=results)

    async def _run_within_budget(self, checks: Sequence[Check]) -> list[CheckResult]:
        deadline = self._deadline()
        results: list[CheckResult] = []
        for index, check in enumerate(checks):
            remaining = self._remaining(deadline)
            if remaining is not None and remaining <= 0:
                results.extend(self._deadline_exceeded(checks[index:]))
                break
            timeout = self._timeout if remaining is None else min(self._timeout, remaining)
            results.append(await run_check(check, timeout=timeout))
        return results

    def _deadline(self) -> float | None:
        if self._overall_timeout is None:
            return None
        return asyncio.get_running_loop().time() + self._overall_timeout

    def _remaining(self, deadline: float | None) -> float | None:
        if deadline is None:
            return None
        return deadline - asyncio.get_running_loop().time()

    def _deadline_exceeded(self, checks: Sequence[Check]) -> list[CheckResult]:
        return [
            CheckResult(
                check_id=check.id,
                status=CheckStatus.ERROR,
                detail=f"overall diagnostics deadline ({self._overall_timeout:g}s) exhausted "
                "before this check ran",
            )
            for check in checks
        ]

    def _can_run(self, check: Check) -> bool:
        if self._worker_mode is None:
            return True
        return check.vantage is not Vantage.WORKER


def _configured_secret_refs() -> list[tuple[str, bool]]:
    """Collect the ``secret=True`` refs the current environment requires as ``(ref, is_platform)``.

    A setting is checked only when its ``required_when`` predicate holds against the same
    environment snapshot the registry resolves against — the contract :func:`config.validate`
    enforces at startup. This scopes ``secret_ref`` to the refs the deployment actually depends
    on (only the settings whose ``required_when`` predicate holds) instead of flagging a
    provider-default ref no active provider needs.

    Every ``KDIVE_*`` secret setting is operator-owned platform config (not tenant data), so
    each is flagged ``is_platform=True`` — naming an unresolved one in the verdict is safe.
    Per-tenant refs (which must never be named) live in the secret registry, not config, and are
    folded in by a later wave; the framework already enforces non-disclosure for them.
    """
    env = config.env_snapshot()
    refs: list[tuple[str, bool]] = []
    for setting in config.all_settings():
        if not setting.secret or not setting.required_when(env):
            continue
        value = config.get(setting)
        if value:
            refs.append((value, True))
    return refs


def _secret_ref_check() -> SecretRefCheck:
    root = Path(config.require(SECRETS_ROOT))
    refs = _configured_secret_refs()

    def _resolve(ref: str) -> None:
        if not root.is_dir():
            raise _SecretBackendUnreachable(str(root))
        try:
            read_secret_file(root, ref)
        except PathSafetyError:
            raise FileNotFoundError(ref) from None

    return SecretRefCheck(
        refs=refs, resolve=_resolve, backend_unreachable=_SecretBackendUnreachable
    )


def _worker_vantage_checks(
    descriptors: Sequence[WorkerVantageDescriptor],
) -> list[WorkerVantageCheck]:
    return [
        WorkerVantageCheck(id=descriptor.id, provider=descriptor.provider)
        for descriptor in descriptors
    ]


@dataclass(frozen=True, slots=True)
class _EnabledDiagnosticContribution:
    contribution: DiagnosticProviderContribution
    unavailable_worker_checks: tuple[WorkerVantageDescriptor, ...]


def _enabled_provider_contributions(
    provider_contributions: Sequence[DiagnosticProviderContribution],
) -> list[_EnabledDiagnosticContribution]:
    enabled: list[_EnabledDiagnosticContribution] = []
    for contribution in provider_contributions:
        if contribution.enabled():
            enabled.append(
                _EnabledDiagnosticContribution(
                    contribution=contribution,
                    unavailable_worker_checks=tuple(contribution.unavailable_worker_checks()),
                )
            )
    return enabled


def _provider_checks(contributions: Sequence[_EnabledDiagnosticContribution]) -> list[Check]:
    return [check for contribution in contributions for check in contribution.contribution.checks()]


def _provider_egress_checks(contributions: Sequence[_EnabledDiagnosticContribution]) -> list[Check]:
    checks: list[Check] = []
    for contribution in contributions:
        checks.extend(contribution.contribution.egress_checks())
    return checks


def _worker_vantage_mode(
    *,
    contributions: Sequence[_EnabledDiagnosticContribution],
    pool: AsyncConnectionPool | None,
) -> WorkerVantageMode | None:
    if not contributions:
        return None
    if pool is None:
        unavailable_worker_checks = [
            check
            for contribution in contributions
            for check in _worker_vantage_checks(contribution.unavailable_worker_checks)
        ]
        return WorkerVantageSubstitutionMode(
            WorkerVantageSubstitution.FEATURE_NOT_ENABLED,
            unavailable_worker_checks,
        )
    return _worker_vantage_dispatch_mode(contributions=contributions, pool=pool)


def _worker_vantage_dispatch_mode(
    *,
    contributions: Sequence[_EnabledDiagnosticContribution],
    pool: AsyncConnectionPool,
) -> WorkerVantageDispatchMode:
    # Function-local import: worker_dispatch imports WORKER_UNAVAILABLE_DETAIL from this module,
    # so a top-level import here would be a cycle (ADR-0164).
    from kdive.diagnostics.worker_dispatch import JobWorkerCheckDispatcher

    dispatchers = [
        JobWorkerCheckDispatcher(
            pool,
            provider=contribution.contribution.provider,
            worker_check_ids=tuple(
                descriptor.id for descriptor in contribution.unavailable_worker_checks
            ),
        )
        for contribution in contributions
    ]
    dispatcher = (
        dispatchers[0] if len(dispatchers) == 1 else _CompositeWorkerCheckDispatcher(dispatchers)
    )
    return WorkerVantageDispatchMode(dispatcher)


def default_service_factory(
    provider: str | None,
    *,
    with_egress: bool = False,
    pool: AsyncConnectionPool | None = None,
    provider_contributions: Sequence[DiagnosticProviderContribution] = (),
) -> DiagnosticsService:
    """Build the production read-only diagnostics service for ``provider``.

    Assembles the server-vantage ``secret_ref`` check over the configured secret refs, resolved
    against the file-ref backend under ``KDIVE_SECRETS_ROOT``. When a ``[[remote_libvirt]]``
    instance is declared, the provider diagnostic contribution also assembles the server-vantage
    ``remote_libvirt_reachability`` and ``remote_libvirt_base_image_staging`` checks (ADR-0125,
    ADR-0150) without this generic service constructing provider-specific checks directly.

    The worker-vantage ``provider_tls``/``gdbstub_acl`` checks run on the worker via a
    :class:`~kdive.diagnostics.worker_dispatch.JobWorkerCheckDispatcher` when ``pool`` is supplied
    and remote-libvirt is configured (ADR-0164): the service bounded-waits for the dispatched job
    and merges its real three-state results, surfacing ``WORKER_UNAVAILABLE`` only when the worker
    does not pick the job up in time. When no ``pool`` is supplied (no dispatch wired), the
    worker-vantage checks keep the honest ``FEATURE_NOT_ENABLED`` substitution
    (``failure_category=not_implemented``, ADR-0139) instead of a fabricated verdict.

    ``with_egress`` opts into provider-owned heavy mutating ``guest_egress`` probes. A provider
    contribution supplies those checks only when that deployment has a real probe-guest seam wired;
    otherwise the opt-in fails fast instead of silently dropping the requested check.

    Raises:
        CategorizedError: ``with_egress`` is requested but no enabled provider contribution
            supplies an egress check in this deployment (``CONFIGURATION_ERROR``).
    """
    enabled_contributions = _enabled_provider_contributions(provider_contributions)
    egress_checks = _provider_egress_checks(enabled_contributions) if with_egress else []
    if with_egress and not egress_checks:
        raise CategorizedError(
            "guest_egress (--with-egress) needs a provider contribution with a real "
            "probe-guest seam; none is wired in this deployment (ADR-0091)",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    checks: list[Check] = [
        _secret_ref_check(),
        *_provider_checks(enabled_contributions),
        *egress_checks,
    ]
    worker_mode = _worker_vantage_mode(contributions=enabled_contributions, pool=pool)
    # When a dispatcher is wired it owns the worker-vantage outcome; otherwise FEATURE_NOT_ENABLED
    # keeps the substituted detail honest (provider_tls/gdbstub_acl are unwired here, not a worker
    # outage) — ADR-0139.
    return DiagnosticsService(
        checks=checks,
        per_check_timeout=_DEFAULT_PER_CHECK_TIMEOUT,
        overall_timeout=_DEFAULT_OVERALL_TIMEOUT,
        worker_mode=worker_mode,
    )
