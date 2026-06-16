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
from pathlib import Path

import kdive.config as config
import kdive.diagnostics.reachability as reachability
from kdive.config.core_settings import SECRETS_ROOT
from kdive.diagnostics.checks import (
    Check,
    CheckResult,
    CheckStatus,
    GdbstubAclCheck,
    ProviderTlsCheck,
    RemoteLibvirtReachabilityCheck,
    SecretRefCheck,
    TlsProbeOutcome,
    Vantage,
    run_check,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.config import is_remote_libvirt_configured
from kdive.security.secrets.paths import PathSafetyError
from kdive.security.secrets.secrets import read_secret_file

_REMOTE_PROVIDER = "remote-libvirt"

WORKER_UNAVAILABLE_DETAIL = "worker could not pick up the diagnostic job; check /livez and /readyz"

_DEFAULT_PER_CHECK_TIMEOUT = 10.0
_DEFAULT_OVERALL_TIMEOUT = 30.0


class _SecretBackendUnreachable(Exception):
    """The secret backend root is absent — a check-cannot-run condition, not a per-ref miss."""


def worker_unavailable_results(checks: Sequence[Check]) -> list[CheckResult]:
    """Return an ``error`` result per worker-vantage check when the worker is down.

    The result points at the health endpoints (ADR-0090) rather than hanging on the job
    queue — the diagnostic that explains breakage must not wedge on the breakage it exists
    to explain (ADR-0091 §1). It is an ``error``, never a contract ``fail`` (no fix string).
    """
    return [
        CheckResult(
            check_id=check.id,
            status=CheckStatus.ERROR,
            detail=WORKER_UNAVAILABLE_DETAIL,
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
                ``False``, worker-vantage checks are not run — they surface as ``error``
                pointing at the health endpoints (ADR-0091 §1).
        """
        self._checks = list(checks)
        self._timeout = per_check_timeout
        self._overall_timeout = overall_timeout
        self._worker_available = worker_available

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
        results.extend(worker_unavailable_results(skipped))
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


async def _never_tls(_ca_path: str) -> TlsProbeOutcome:
    # Never invoked: the check is substituted under worker_available=False. The raise guards a
    # future wiring mistake (running a worker-vantage check without a real probe).
    raise NotImplementedError("provider_tls has no worker-job probe in this deployment")


async def _never_acl(_host: str, _range: str) -> bool | None:
    raise NotImplementedError("gdbstub_acl has no worker-job probe in this deployment")


def _remote_libvirt_checks() -> list[Check]:
    """Assemble the remote-libvirt diagnostic checks when an instance is declared.

    The server-vantage ``remote_libvirt_reachability`` check is the concrete probe (ADR-0125); it
    resolves config lazily at run time. The worker-vantage ``provider_tls``/``gdbstub_acl`` checks
    are **named** but have no worker-job probe in this slice, so they are built with empty fields
    and a never-called probe — the service substitutes them with the honest worker-unavailable
    error (``worker_available=False``) rather than fabricating a "host unreachable" verdict.
    """
    reachability_check = RemoteLibvirtReachabilityCheck(
        provider=_REMOTE_PROVIDER,
        probe=reachability.remote_libvirt_reachability_probe(),
    )
    tls_check = ProviderTlsCheck(provider=_REMOTE_PROVIDER, ca_path="", probe=_never_tls)
    acl_check = GdbstubAclCheck(provider=_REMOTE_PROVIDER, host="", port_range="", probe=_never_acl)
    return [reachability_check, tls_check, acl_check]


def default_service_factory(
    provider: str | None, *, with_egress: bool = False
) -> DiagnosticsService:
    """Build the production read-only diagnostics service for ``provider``.

    Assembles the server-vantage ``secret_ref`` check over the configured secret refs, resolved
    against the file-ref backend under ``KDIVE_SECRETS_ROOT``. When a ``[[remote_libvirt]]``
    instance is declared (``is_remote_libvirt_configured()``), it also assembles the server-vantage
    ``remote_libvirt_reachability`` check (ADR-0125, the qemu+tls reachability probe) and the
    worker-vantage ``provider_tls``/``gdbstub_acl`` checks.

    The service is built with ``worker_available=False``: this slice wires no worker-job dispatch,
    so the worker-vantage checks surface as an honest ``error`` ("worker could not pick up the
    diagnostic job; check /livez and /readyz") rather than a fabricated probe verdict. The
    server-vantage ``secret_ref`` and ``remote_libvirt_reachability`` checks are unaffected by the
    flag and still run.

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
    if is_remote_libvirt_configured():
        checks.extend(_remote_libvirt_checks())
    # worker_available=False is load-bearing: _remote_libvirt_checks builds the worker-vantage
    # TLS/ACL checks with empty fields + never-called probes on the contract that they are
    # substituted (never run) here. A future flip to True must replace those placeholder
    # constructions with real probes (see ADR-0125's worker-job follow-up) — do not flip it alone.
    return DiagnosticsService(
        checks=checks,
        per_check_timeout=_DEFAULT_PER_CHECK_TIMEOUT,
        overall_timeout=_DEFAULT_OVERALL_TIMEOUT,
        worker_available=False,
    )
