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

import kdive.config as config
import kdive.diagnostics.reachability as reachability
from kdive.config.core_settings import SECRETS_ROOT
from kdive.diagnostics.checks import (
    GDBSTUB_ACL_ID,
    PROVIDER_TLS_ID,
    Check,
    CheckResult,
    CheckStatus,
    RemoteLibvirtReachabilityCheck,
    SecretRefCheck,
    Vantage,
    run_check,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.config import is_remote_libvirt_configured
from kdive.security.secrets.paths import PathSafetyError
from kdive.security.secrets.secrets import read_secret_file

_REMOTE_PROVIDER = "remote-libvirt"

WORKER_UNAVAILABLE_DETAIL = "worker could not pick up the diagnostic job; check /livez and /readyz"
FEATURE_NOT_ENABLED_DETAIL = (
    "worker-vantage diagnostic checks (provider_tls, gdbstub_acl) are not enabled "
    "in this deployment"
)

# failure_category labels for the substituted worker-vantage results (ADR-0139). Kept as plain
# strings, mirroring kdive.diagnostics.checks, so this module stays free of a domain-errors import.
_TRANSPORT_FAILURE = "transport_failure"
_NOT_IMPLEMENTED = "not_implemented"

_DEFAULT_PER_CHECK_TIMEOUT = 10.0
_DEFAULT_OVERALL_TIMEOUT = 30.0


class WorkerVantageSubstitution(StrEnum):
    """Why a worker-vantage check is substituted instead of run (ADR-0139).

    The two causes carry **distinct** details and ``failure_category`` labels so an operator —
    and a programmatic caller — can tell them apart without parsing prose:

    - ``WORKER_UNAVAILABLE`` — dispatch exists but the worker cannot pick the job up; the detail
      points at the health endpoints (ADR-0090). This is the historical meaning of a bare
      ``worker_available=False`` and stays the default.
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
_SUBSTITUTION_CATEGORY: dict[WorkerVantageSubstitution, str] = {
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
        worker_available: bool = True,
        substitution_reason: WorkerVantageSubstitution = (
            WorkerVantageSubstitution.WORKER_UNAVAILABLE
        ),
        unavailable_worker_checks: Sequence[WorkerVantageCheck] = (),
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
            worker_available: Whether the worker can pick up worker-vantage jobs. When
                ``False``, worker-vantage checks are not run — they surface as a substituted
                ``error`` whose cause is named by ``substitution_reason`` (ADR-0091 §1).
            substitution_reason: Why a worker-vantage check is substituted when
                ``worker_available`` is ``False`` (ADR-0139). Defaults to ``WORKER_UNAVAILABLE``
                (the health-endpoint detail), preserving the historical meaning of a bare
                ``worker_available=False``; pass ``FEATURE_NOT_ENABLED`` when no worker-job
                dispatch is wired so the detail does not misread as a worker outage.
            unavailable_worker_checks: Worker-vantage diagnostics that this deployment cannot
                run at all. They are explicit result metadata, not runnable ``Check`` objects.
        """
        self._checks = list(checks)
        self._timeout = per_check_timeout
        self._overall_timeout = overall_timeout
        self._worker_available = worker_available
        self._substitution_reason = substitution_reason
        self._unavailable_worker_checks = list(unavailable_worker_checks)

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
        results.extend(worker_unavailable_results(skipped, self._substitution_reason))
        results.extend(
            worker_unavailable_results(
                self._unavailable_worker_checks,
                self._substitution_reason,
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
        return self._worker_available or check.vantage is not Vantage.WORKER


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


def _remote_libvirt_checks() -> list[Check]:
    """Assemble the remote-libvirt diagnostic checks when an instance is declared.

    The server-vantage ``remote_libvirt_reachability`` check is the concrete probe (ADR-0125); it
    resolves config lazily at run time. Worker-vantage checks are reported separately as explicit
    unavailable metadata until worker-job dispatch is wired for them (ADR-0139).
    """
    return [
        RemoteLibvirtReachabilityCheck(
            provider=_REMOTE_PROVIDER,
            probe=reachability.remote_libvirt_reachability_probe(),
        )
    ]


def _remote_libvirt_unavailable_worker_checks() -> list[WorkerVantageCheck]:
    """Name worker-vantage remote-libvirt diagnostics that this deployment cannot run."""
    return [
        WorkerVantageCheck(id=PROVIDER_TLS_ID, provider=_REMOTE_PROVIDER),
        WorkerVantageCheck(id=GDBSTUB_ACL_ID, provider=_REMOTE_PROVIDER),
    ]


def default_service_factory(
    provider: str | None, *, with_egress: bool = False
) -> DiagnosticsService:
    """Build the production read-only diagnostics service for ``provider``.

    Assembles the server-vantage ``secret_ref`` check over the configured secret refs, resolved
    against the file-ref backend under ``KDIVE_SECRETS_ROOT``. When a ``[[remote_libvirt]]``
    instance is declared (``is_remote_libvirt_configured()``), it also assembles the server-vantage
    ``remote_libvirt_reachability`` check (ADR-0125, the qemu+tls reachability probe) and the
    explicit unavailable-worker metadata for ``provider_tls``/``gdbstub_acl``.

    The service is built with ``worker_available=False`` and
    ``substitution_reason=FEATURE_NOT_ENABLED``: this slice wires no worker-job dispatch, so the
    worker-vantage checks surface as an honest ``error`` ("worker-vantage diagnostic checks ... are
    not enabled in this deployment", ``failure_category=not_implemented``) rather than a fabricated
    probe verdict or the worker-down ``/livez``/``/readyz`` detail — a running worker is not the
    cause (ADR-0139, #484). The server-vantage ``secret_ref`` and ``remote_libvirt_reachability``
    checks are unaffected by the flag and still run.

    ``with_egress`` opts into the heavy mutating ``guest_egress`` probe, which provisions a
    short-lived guest on the target provider. Its production probe-guest seam needs a bootable
    image on that provider — on remote-libvirt that image is **operator-staged** until the
    M2.4 image-lifecycle work makes it first-class (ADR-0091), so this default factory raises
    a fail-fast configuration error rather than silently dropping the opt-in check; a
    deployment that has staged the image wires a probe-guest-backed factory in its place.

    Raises:
        CategorizedError: ``with_egress`` is requested but no probe-guest image/seam is wired
            in this deployment (``CONFIGURATION_ERROR``).
    """
    if with_egress:
        raise CategorizedError(
            "guest_egress (--with-egress) needs an operator-staged probe-guest image on the "
            "target provider; none is wired in this deployment (ADR-0091, M2.4)",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    checks: list[Check] = [_secret_ref_check()]
    unavailable_worker_checks: list[WorkerVantageCheck] = []
    if is_remote_libvirt_configured():
        checks.extend(_remote_libvirt_checks())
        unavailable_worker_checks.extend(_remote_libvirt_unavailable_worker_checks())
    # FEATURE_NOT_ENABLED makes the substituted detail say provider_tls/gdbstub_acl are unwired
    # here, not that a worker is down. The worker-job follow-up replaces
    # unavailable_worker_checks with concrete worker-vantage checks and drops this reason.
    return DiagnosticsService(
        checks=checks,
        per_check_timeout=_DEFAULT_PER_CHECK_TIMEOUT,
        overall_timeout=_DEFAULT_OVERALL_TIMEOUT,
        worker_available=False,
        substitution_reason=WorkerVantageSubstitution.FEATURE_NOT_ENABLED,
        unavailable_worker_checks=unavailable_worker_checks,
    )
