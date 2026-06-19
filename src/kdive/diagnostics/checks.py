"""The `Check` framework and the three read-only diagnostic checks (ADR-0091 §2).

A `Check` is an `id`, a `vantage`, and an async `run() -> CheckResult`, where
`CheckResult.status` is **three-state**: `pass` (the contract holds), `fail` (the
contract is violated and `fix` names the exact remediation), and `error` (the check
could not be run to a verdict — the backend was down, the host was unreachable, the
probe timed out — and `detail` says what blocked it, *never* a contract-fix string).
Collapsing `error` into `fail` is the worst failure a diagnostic can have: it would emit
a confident wrong fix from the one tool whose value is naming the right one.

Every check runs through :func:`run_check`, which bounds it by a per-check timeout (a
check that does not answer is `error`, not a hang) and converts any unexpected
exception into `error` — so a check can never wedge or crash the aggregating service.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

SECRET_REF_ID = "secret_ref"
PROVIDER_TLS_ID = "provider_tls"
GDBSTUB_ACL_ID = "gdbstub_acl"
REACHABILITY_ID = "remote_libvirt_reachability"
BASE_IMAGE_STAGING_ID = "remote_libvirt_base_image_staging"
LOCAL_KERNEL_SRC_ID = "local_kernel_src"
BUILDHOST_AGENT_ID = "ephemeral_libvirt_buildhost_agent"

# The remediation the ephemeral build-host agent check surfaces as its ``fix`` (ADR-0167). Owned in
# diagnostics (diagnostic-output policy), like LOCAL_KERNEL_SRC_FIX / BASE_VOLUME_NOT_STAGED_FIX:
# the only legal import direction out of diagnostics is to providers/db, so the policy lives here.
BUILDHOST_AGENT_FIX = (
    "an ephemeral_libvirt build host's throwaway builder boots but its qemu-guest-agent never "
    "becomes usable; rebuild or repair the operator-staged base build image so its guest agent "
    "starts (docs/operating/build-source-staging.md), then re-run doctor --with-buildhost-agent"
)

# The build-lane remediation the local-kernel-src check surfaces as its ``fix`` (ADR-0163). It is
# owned here, not in the provider's ``workspace.py``: ``diagnostics → providers`` is the only legal
# import direction, so the diagnostic output policy lives in diagnostics. It names the same two
# lanes as ``workspace.py``'s ``_BUILD_LANE_GUIDANCE`` (stage a warm tree + set
# ``KDIVE_KERNEL_SRC``, or register a git build host) as an independent literal, so ``checks``
# stays free of a provider import.
LOCAL_KERNEL_SRC_FIX = (
    "stage a kernel source tree on the build worker and set KDIVE_KERNEL_SRC to its absolute "
    "path (docs/operating/build-source-staging.md), or route builds to a registered git build "
    "host (build_hosts.register_ssh / build_hosts.register_ephemeral_libvirt)"
)

# The operator remediation the base-image-staging check surfaces as its ``fix`` (ADR-0080,
# ADR-0150). It is owned here, not in the provider's ``storage.py``: ``diagnostics → providers``
# is the only legal import direction, so the diagnostic output policy lives in diagnostics. It
# describes the same operator action as ``storage.py``'s provision-time error message.
BASE_VOLUME_NOT_STAGED_FIX = (
    "base image volume is not staged on the remote host's storage pool; stage the "
    "operator-provided base image volume on the configured pool (ADR-0080), then retry"
)

# The failure-category labels the reachability verdict carries (ADR-0125). They mirror the
# ``ErrorCategory`` the underlying connection raises, kept as plain strings so ``checks`` stays
# free of provider/transport imports.
_TRANSPORT_FAILURE = "transport_failure"
_CONFIGURATION_ERROR = "configuration_error"

_log = logging.getLogger(__name__)


class CheckStatus(StrEnum):
    """The three-state verdict of a single check (ADR-0091 §2)."""

    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"


class Vantage(StrEnum):
    """Where a check must run from to observe the contract it probes.

    ``kdivectl`` on an operator laptop cannot see the worker→hypervisor TLS chain, so a
    check declares its vantage and the deployment runs it from there (ADR-0091 §1).
    """

    SERVER = "server"
    WORKER = "worker"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One check's three-state verdict (ADR-0091 §2).

    Args:
        check_id: The stable id of the check that produced this result.
        status: The three-state verdict.
        detail: On ``fail``, what contract is violated; on ``error``, what *blocked* the
            check (never a fix string); on ``pass``, a short confirmation.
        fix: The exact remediation — mandatory on ``fail``, forbidden otherwise (an
            ``error``/``pass`` carrying a fix is a producer bug).
        provider: The provider this result pertains to, or ``None`` for a
            provider-independent check (``secret_ref``).
        failure_category: The :class:`ErrorCategory`-style label for *why* the contract was
            violated (``fail``) or the check could not run (``error``) — e.g.
            ``transport_failure`` vs ``configuration_error`` for a reachability probe. ``None``
            on ``pass`` (a clean read has no failure to categorize); a ``pass`` carrying one is a
            producer bug, mirroring the ``fix``-only-on-``fail`` rule.
    """

    check_id: str
    status: CheckStatus
    detail: str
    fix: str | None = None
    provider: str | None = None
    failure_category: str | None = None

    def __post_init__(self) -> None:
        if self.status is CheckStatus.FAIL and not self.fix:
            raise ValueError(f"{self.check_id}: a fail result must name a fix")
        if self.status is not CheckStatus.FAIL and self.fix is not None:
            raise ValueError(
                f"{self.check_id}: only a fail result may carry a fix "
                f"(status {self.status.value!r} carried {self.fix!r})"
            )
        if self.status is CheckStatus.PASS and self.failure_category is not None:
            raise ValueError(
                f"{self.check_id}: a pass result must not carry a failure_category "
                f"(carried {self.failure_category!r})"
            )


class Check(ABC):
    """A single diagnostic probe with an explicit vantage and a three-state verdict."""

    @property
    @abstractmethod
    def id(self) -> str:
        """The stable check id (e.g. ``secret_ref``)."""

    @property
    @abstractmethod
    def vantage(self) -> Vantage:
        """Where this check must run from."""

    @abstractmethod
    async def run(self) -> CheckResult:
        """Probe the contract and return a three-state result.

        Implementations return ``error`` for an indeterminate run rather than raising;
        :func:`run_check` is the backstop that maps a leaked exception or timeout to
        ``error`` so the aggregating service can never wedge.
        """


async def run_check(check: Check, *, timeout: float) -> CheckResult:
    """Run ``check`` bounded by ``timeout``; map a timeout or unexpected error to ``error``.

    A check that does not answer within ``timeout`` is an ``error`` with a
    "did not respond within N" detail — never a hang and never a contract ``fail``. Any
    exception the check leaks is also mapped to ``error`` with a generic blocked-reason
    detail (the exception text is not surfaced, so an unexpected backend message cannot
    leak through the verdict).
    """
    try:
        async with asyncio.timeout(timeout):
            return await check.run()
    except TimeoutError:
        return CheckResult(
            check_id=check.id,
            status=CheckStatus.ERROR,
            detail=f"check did not respond within {timeout:g}s",
        )
    except Exception as exc:  # noqa: BLE001 - backstop: a leaked error must not wedge the service
        _log.error("diagnostic check %s raised unexpectedly: %s", check.id, exc, exc_info=True)
        return CheckResult(
            check_id=check.id,
            status=CheckStatus.ERROR,
            detail="check could not be run to a verdict (unexpected error)",
        )


# A resolver raises on an unresolved ref; the secret backend's own unreachable-exception
# type (passed separately) is the error-vs-fail discriminator.
SecretResolve = Callable[[str], object]


def _redact_exception_args(exc: Exception) -> None:
    """Remove ref-bearing exception args before traceback logging formats the exception."""
    with contextlib.suppress(Exception):
        exc.args = (f"{type(exc).__name__} while resolving configured secret ref",)


class SecretRefCheck(Check):
    """Server-vantage: every configured secret ref resolves in the backend (ADR-0091 §2).

    Full coverage spans both platform and per-tenant refs (the motivating M2 fault did not
    assume which kind). Non-disclosure is enforced on the **reporting** surface: the verdict
    reports aggregate pass/fail counts and platform-ref detail only — a per-tenant ref that
    fails to resolve is counted but its identifier is never surfaced, so the diagnostic
    catches every unresolved ref without becoming a cross-tenant secret-presence disclosure.

    A backend that cannot be reached at all (``backend_unreachable`` raised) is ``error``,
    not a contract ``fail`` — the refs may all be fine.
    """

    def __init__(
        self,
        *,
        refs: Sequence[tuple[str, bool]],
        resolve: SecretResolve,
        backend_unreachable: type[Exception] | tuple[type[Exception], ...] = (),
    ) -> None:
        """Build the check.

        Args:
            refs: ``(ref, is_platform)`` pairs for every configured secret ref. The
                ``is_platform`` flag gates whether the ref identifier may appear in
                ``detail`` (platform refs are operator-owned config, not tenant data).
            resolve: Resolves one ref, raising on a ref that does not resolve.
            backend_unreachable: Exception type(s) signalling the backend itself is
                unreachable (→ ``error``), distinct from a per-ref miss (→ ``fail``).
        """
        self._refs = list(refs)
        self._resolve = resolve
        self._unreachable = backend_unreachable

    @property
    def id(self) -> str:
        return SECRET_REF_ID

    @property
    def vantage(self) -> Vantage:
        return Vantage.SERVER

    async def run(self) -> CheckResult:
        unresolved_platform: list[str] = []
        unresolved_count = 0
        try:
            for ref, is_platform in self._refs:
                if not await self._resolves(ref, is_platform=is_platform):
                    unresolved_count += 1
                    if is_platform:
                        unresolved_platform.append(ref)
        except self._unreachable_types():
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.ERROR,
                detail="secret backend unreachable; cannot verify any ref",
            )
        return self._verdict(unresolved_count, unresolved_platform)

    async def _resolves(self, ref: str, *, is_platform: bool) -> bool:
        try:
            await asyncio.to_thread(self._resolve, ref)
        except self._unreachable_types():
            raise
        except Exception as exc:  # noqa: BLE001 - any per-ref resolution failure is unresolved
            _redact_exception_args(exc)
            _log.warning(
                "secret_ref resolver failed for %s ref: %s",
                "platform" if is_platform else "non-platform",
                type(exc).__name__,
                exc_info=True,
            )
            return False
        return True

    def _unreachable_types(self) -> tuple[type[Exception], ...]:
        if isinstance(self._unreachable, tuple):
            return self._unreachable
        return (self._unreachable,)

    def _verdict(self, unresolved: int, unresolved_platform: list[str]) -> CheckResult:
        total = len(self._refs)
        if unresolved == 0:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.PASS,
                detail=f"all {total} configured secret refs resolve",
            )
        platform_detail = (
            f" (unresolved platform refs: {', '.join(sorted(unresolved_platform))})"
            if unresolved_platform
            else ""
        )
        return CheckResult(
            check_id=self.id,
            status=CheckStatus.FAIL,
            detail=f"{unresolved} of {total} configured secret refs do not resolve"
            + platform_detail,
            fix=(
                "secret ref does not resolve under KDIVE_SECRETS_ROOT; "
                "create the file-ref or fix the path"
            ),
        )


class TlsProbeOutcome(StrEnum):
    """The three observable outcomes of a provider TLS probe."""

    VALID = "valid"
    INVALID = "invalid"
    UNREACHABLE = "unreachable"


TlsProbe = Callable[[str], Awaitable[TlsProbeOutcome]]


class ProviderTlsCheck(Check):
    """Worker-vantage: the provider TLS chain validates against the configured CA.

    Host-unreachable is ``error`` (the chain may be fine; the host is simply down);
    cert-invalid is ``fail`` with the reissue/CA-path remediation (ADR-0091 §2).
    """

    def __init__(self, *, provider: str, ca_path: str, probe: TlsProbe) -> None:
        self._provider = provider
        self._ca_path = ca_path
        self._probe = probe

    @property
    def id(self) -> str:
        return PROVIDER_TLS_ID

    @property
    def vantage(self) -> Vantage:
        return Vantage.WORKER

    async def run(self) -> CheckResult:
        outcome = await self._probe(self._ca_path)
        if outcome is TlsProbeOutcome.VALID:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.PASS,
                detail=f"provider TLS chain validates against {self._ca_path}",
                provider=self._provider,
            )
        if outcome is TlsProbeOutcome.UNREACHABLE:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.ERROR,
                detail="provider host unreachable; cannot validate the TLS chain",
                provider=self._provider,
            )
        return CheckResult(
            check_id=self.id,
            status=CheckStatus.FAIL,
            detail=f"provider cert not signed by configured CA {self._ca_path}",
            fix=(
                f"provider cert not signed by configured CA {self._ca_path}; "
                "reissue or set KDIVE_PROVIDER_CA"
            ),
            provider=self._provider,
        )


# Returns True if the ACL admits the range, False if blocked, None if indeterminate.
GdbstubAclProbe = Callable[[str, str], Awaitable[bool | None]]


class GdbstubAclCheck(Check):
    """Worker-vantage: the host ACL on ``config.gdb_addr`` admits the gdbstub port range.

    A **policy** check, not a live-port check: the gdbstub port is assigned per-domain
    (ADR-0083), so a cold preflight with zero running guests has no concrete port —
    validating that the ACL admits the configured range needs no live domain and catches
    the M2 fault (a closed ACL) directly. The probe connects to the reserved ACL-probe port
    (the lowest port of the range, never assigned to a System; ADR-0184), so it never pauses a
    live guest; ``port_range`` is the full configured firewall range named in the operator
    message. An indeterminate probe (``None``) is ``error``.
    """

    def __init__(
        self, *, provider: str, host: str, port_range: str, probe: GdbstubAclProbe
    ) -> None:
        self._provider = provider
        self._host = host
        self._port_range = port_range
        self._probe = probe

    @property
    def id(self) -> str:
        return GDBSTUB_ACL_ID

    @property
    def vantage(self) -> Vantage:
        return Vantage.WORKER

    async def run(self) -> CheckResult:
        admitted = await self._probe(self._host, self._port_range)
        if admitted is None:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.ERROR,
                detail=f"could not determine the ACL on {self._host} for {self._port_range}",
                provider=self._provider,
            )
        if admitted:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.PASS,
                detail=f"ACL on {self._host} admits gdbstub range {self._port_range}",
                provider=self._provider,
            )
        return CheckResult(
            check_id=self.id,
            status=CheckStatus.FAIL,
            detail=f"gdbstub port range {self._port_range} on {self._host} blocked",
            fix=(
                f"gdbstub port range {self._port_range} on {self._host} blocked; "
                "open the host firewall / ACL for it"
            ),
            provider=self._provider,
        )


class ReachabilityOutcome(StrEnum):
    """The three observable outcomes of a remote-libvirt reachability probe (ADR-0125).

    ``REACHABLE`` — the ``qemu+tls://`` connection opened and ``getInfo()`` returned.
    ``UNREACHABLE`` — the TLS connect failed (host down / port closed): a contract ``fail``.
    ``MISCONFIGURED`` — the probe could not run (bad URI/cert/inventory): an ``error``, never a
    confident "host down".
    """

    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"
    MISCONFIGURED = "misconfigured"


ReachabilityProbe = Callable[[], Awaitable[ReachabilityOutcome]]


class RemoteLibvirtReachabilityCheck(Check):
    """Server-vantage: the remote-libvirt ``qemu+tls://`` host is libvirt-reachable (ADR-0125).

    The server itself opens the libvirt client connection, so this is ``Vantage.SERVER`` (it must
    run even when the worker is down — that is exactly when an operator needs to know whether the
    host is reachable). The verdict is scoped to **libvirt-reachability**: a reachable-but-
    misconfigured host (no storage pool/network) still reports ``pass`` and surfaces its config
    failure at provision time. Host-down is a contract ``fail`` (``transport_failure``); a probe
    that could not run (bad URI/cert/inventory) is ``error`` (``configuration_error``) — emitting a
    "host down" fix when the operator's own config blocked the probe is the confident-wrong-fix
    failure ADR-0091 forbids.
    """

    def __init__(self, *, provider: str, probe: ReachabilityProbe) -> None:
        self._provider = provider
        self._probe = probe

    @property
    def id(self) -> str:
        return REACHABILITY_ID

    @property
    def vantage(self) -> Vantage:
        return Vantage.SERVER

    async def run(self) -> CheckResult:
        outcome = await self._probe()
        if outcome is ReachabilityOutcome.REACHABLE:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.PASS,
                detail="remote-libvirt host is reachable over qemu+tls (libvirt-reachable only; "
                "config usability still surfaces at provision)",
                provider=self._provider,
            )
        if outcome is ReachabilityOutcome.UNREACHABLE:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.FAIL,
                detail="remote-libvirt host is not reachable over qemu+tls",
                fix=(
                    "remote-libvirt host unreachable; bring the host up and open its libvirt "
                    "TLS port (16514), then retry"
                ),
                provider=self._provider,
                failure_category=_TRANSPORT_FAILURE,
            )
        return CheckResult(
            check_id=self.id,
            status=CheckStatus.ERROR,
            detail="remote-libvirt reachability could not be probed; check the [[remote_libvirt]] "
            "URI, TLS cert refs, and systems.toml inventory",
            provider=self._provider,
            failure_category=_CONFIGURATION_ERROR,
        )


class BaseImageStagingOutcome(StrEnum):
    """The observable outcomes of a remote-libvirt base-image-staging probe (ADR-0150).

    ``STAGED`` — the pool exists and the configured base-image volume is staged.
    ``NOT_STAGED`` — the pool exists but the volume is absent: a contract ``fail``.
    ``UNREACHABLE`` — the ``qemu+tls://`` connect failed (host down / port closed): an ``error``.
    ``INDETERMINATE`` — the probe could not reach a verdict (absent pool, unresolvable inventory,
    a non-staged base image, a storage RPC that failed after open): an ``error``, never a confident
    "volume missing".
    """

    STAGED = "staged"
    NOT_STAGED = "not_staged"
    UNREACHABLE = "unreachable"
    INDETERMINATE = "indeterminate"


BaseImageStagingProbe = Callable[[], Awaitable[BaseImageStagingOutcome]]


class BaseImageStagingCheck(Check):
    """Server-vantage: the operator-staged base-image volume is present on the host pool (ADR-0150).

    Reachability (ADR-0125) proves only that the ``qemu+tls://`` host answers; it explicitly does
    not check usability. This check probes the one operator prerequisite that blocks provisioning —
    the base-image volume staged on the host's storage pool (ADR-0080) — from the same server
    vantage, so an unstaged volume is a ``fail`` (with the staging fix) before a caller burns an
    allocation on the provision-time failure. A missing pool / unresolvable inventory / host-down is
    an ``error``: emitting a stage-the-volume fix for any of those is the confident-wrong-fix
    failure ADR-0091 forbids.
    """

    def __init__(self, *, provider: str, probe: BaseImageStagingProbe) -> None:
        self._provider = provider
        self._probe = probe

    @property
    def id(self) -> str:
        return BASE_IMAGE_STAGING_ID

    @property
    def vantage(self) -> Vantage:
        return Vantage.SERVER

    async def run(self) -> CheckResult:
        outcome = await self._probe()
        if outcome is BaseImageStagingOutcome.STAGED:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.PASS,
                detail="base image volume is staged on the remote host's storage pool",
                provider=self._provider,
            )
        if outcome is BaseImageStagingOutcome.NOT_STAGED:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.FAIL,
                detail="base image volume is not staged on the remote host's storage pool",
                fix=BASE_VOLUME_NOT_STAGED_FIX,
                provider=self._provider,
                failure_category=_CONFIGURATION_ERROR,
            )
        if outcome is BaseImageStagingOutcome.UNREACHABLE:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.ERROR,
                detail="remote-libvirt host unreachable; cannot verify base-image staging",
                provider=self._provider,
                failure_category=_TRANSPORT_FAILURE,
            )
        return CheckResult(
            check_id=self.id,
            status=CheckStatus.ERROR,
            detail="base-image staging could not be probed; check the [[remote_libvirt]] "
            "base_image / [[image]] staged volume, the storage pool, and the inventory",
            provider=self._provider,
            failure_category=_CONFIGURATION_ERROR,
        )


class WarmTreeSourceOutcome(StrEnum):
    """The three observable outcomes of a local warm-tree source probe (ADR-0163, ADR-0161).

    ``USABLE`` — ``KDIVE_KERNEL_SRC`` points at an existing absolute directory.
    ``UNSET`` — it is unset/empty/whitespace: a contract ``fail``.
    ``INVALID`` — it is set but not an existing absolute tree: a contract ``fail``.

    There is no indeterminate outcome: a config read plus a local ``stat`` always reaches a
    verdict, so this check has no ``error`` branch (unlike the libvirt probes, whose RPC can be
    unreachable).
    """

    USABLE = "usable"
    UNSET = "unset"
    INVALID = "invalid"


WarmTreeSourceProbe = Callable[[], Awaitable[WarmTreeSourceOutcome]]


async def _always_enabled() -> bool:
    """Default enabled probe: assume the local build host is enabled (ADR-0167).

    Keeps `LocalKernelSrcCheck`'s prior behavior for unit tests and any pool-free assembly; the
    production factory injects a probe that reads the seeded host's `enabled` flag via the pool.
    """
    return True


class LocalKernelSrcCheck(Check):
    """Server-vantage: the seeded local build host's warm-tree source is usable (ADR-0163).

    ``ops.diagnostics`` validated the remote-libvirt runtime provider and the secret backend but
    had no check touching any build host, so it reported healthy while every local warm-tree build
    failed deterministically on an unusable ``KDIVE_KERNEL_SRC`` (#532). The seeded ``worker-local``
    ``LOCAL`` host is a database invariant, so this check is always assembled; it resolves
    ``KDIVE_KERNEL_SRC`` over the same ``warm_tree_source_error`` predicate the build-time
    ``sync_tree`` and admission-time ``check_warm_tree_source_admission`` enforce (ADR-0161), via an
    injected probe so the check holds the three-state policy and is unit-tested without the
    filesystem or config.

    The verdict is provider-independent (``provider=None``, like ``secret_ref``): it is a property
    of the build worker, not a runtime provider. ``PASS`` asserts source-path usability, not that
    the directory is a valid/buildable kernel tree — the predicate does not inspect tree contents.
    It reads the server process's ``KDIVE_KERNEL_SRC``, which is correct when server and worker
    share an environment (the default single-host / compose deployment); a split deployment is the
    worker-vantage refinement (ADR-0163 "Considered & rejected").
    """

    def __init__(
        self,
        *,
        probe: WarmTreeSourceProbe,
        enabled_probe: Callable[[], Awaitable[bool]] = _always_enabled,
    ) -> None:
        self._probe = probe
        self._enabled_probe = enabled_probe

    @property
    def id(self) -> str:
        return LOCAL_KERNEL_SRC_ID

    @property
    def vantage(self) -> Vantage:
        return Vantage.SERVER

    async def run(self) -> CheckResult:
        if not await self._enabled_probe():
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.PASS,
                detail="the seeded local build host is disabled; KDIVE_KERNEL_SRC is not required "
                "(n/a — no local warm-tree lane to validate)",
            )
        outcome = await self._probe()
        if outcome is WarmTreeSourceOutcome.USABLE:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.PASS,
                detail="warm-tree kernel source is set on the build worker "
                "(KDIVE_KERNEL_SRC points at an existing absolute tree)",
            )
        if outcome is WarmTreeSourceOutcome.UNSET:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.FAIL,
                detail="the local build worker has no warm-tree kernel source: KDIVE_KERNEL_SRC "
                "is unset, so every local warm-tree build fails",
                fix=LOCAL_KERNEL_SRC_FIX,
                failure_category=_CONFIGURATION_ERROR,
            )
        return CheckResult(
            check_id=self.id,
            status=CheckStatus.FAIL,
            detail="KDIVE_KERNEL_SRC is set on the build worker but is not an absolute path to an "
            "existing kernel source tree, so every local warm-tree build fails",
            fix=LOCAL_KERNEL_SRC_FIX,
            failure_category=_CONFIGURATION_ERROR,
        )


class BuildHostAgentOutcome(StrEnum):
    """The per-host observable outcomes of the ephemeral build-host agent probe (ADR-0167).

    ``AGENT_READY`` — the builder booted, its guest agent connected, and a trivial command ran.
    ``AGENT_UNREACHABLE`` — the builder started but never reached a usable agent (the agent never
    connected, the trivial command returned non-zero, or the agent dropped mid-exec): a contract
    ``fail``. ``HOST_UNREACHABLE`` — the host/config could not be reached before the agent connected
    (TLS down, missing pool/base image, a probe already in flight): an ``error``, never a confident
    "agent broken".
    """

    AGENT_READY = "agent_ready"
    AGENT_UNREACHABLE = "agent_unreachable"
    HOST_UNREACHABLE = "host_unreachable"


@dataclass(frozen=True, slots=True)
class BuildHostProbeResult:
    """One probed host's outcome.

    Args:
        host_name: The build host's name (named in a ``fail``/``error`` detail).
        outcome: The per-host three-state outcome.
        transport_error: Marks a ``HOST_UNREACHABLE`` that was a transport drop (vs a config cause),
            for the deterministic aggregate ``failure_category`` rule. Ignored for other outcomes.
    """

    host_name: str
    outcome: BuildHostAgentOutcome
    transport_error: bool = False


BuildHostAgentProbe = Callable[[], Awaitable[list[BuildHostProbeResult]]]


class EphemeralLibvirtBuildHostAgentCheck(Check):
    """Server-vantage: every ephemeral_libvirt build host's builder reaches its guest agent.

    Aggregates the per-host outcomes from an injected probe into one three-state verdict (the
    ``secret_ref`` precedent for many sub-probes → one result): any ``AGENT_UNREACHABLE`` →
    ``fail`` (a build routed there fails deterministically); else any ``HOST_UNREACHABLE`` or no
    hosts → ``error`` (an indeterminate or absent target is never a confident ``fail`` and never a
    silent ``pass``); else ``pass``. The aggregate ``error`` ``failure_category`` is
    ``transport_failure`` only when every error cause was a transport drop, else
    ``configuration_error`` — a fixed rule, so the category is stable for programmatic triage.
    """

    def __init__(self, *, probe: BuildHostAgentProbe) -> None:
        self._probe = probe

    @property
    def id(self) -> str:
        return BUILDHOST_AGENT_ID

    @property
    def vantage(self) -> Vantage:
        return Vantage.SERVER

    async def run(self) -> CheckResult:
        results = await self._probe()
        failed = sorted(
            r.host_name for r in results if r.outcome is BuildHostAgentOutcome.AGENT_UNREACHABLE
        )
        if failed:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.FAIL,
                detail="ephemeral_libvirt build host(s) reachable but their guest agent never "
                f"became usable: {', '.join(failed)}",
                fix=BUILDHOST_AGENT_FIX,
                failure_category=_CONFIGURATION_ERROR,
            )
        unreachable = [r for r in results if r.outcome is BuildHostAgentOutcome.HOST_UNREACHABLE]
        if unreachable or not results:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.ERROR,
                detail=self._error_detail(unreachable, results),
                failure_category=self._error_category(unreachable),
            )
        return CheckResult(
            check_id=self.id,
            status=CheckStatus.PASS,
            detail=f"all {len(results)} ephemeral_libvirt build host(s) reached their guest agent",
        )

    @staticmethod
    def _error_detail(
        unreachable: list[BuildHostProbeResult], results: list[BuildHostProbeResult]
    ) -> str:
        if not results:
            return "no ephemeral_libvirt build host is registered; nothing to probe"
        names = ", ".join(sorted(r.host_name for r in unreachable))
        return f"ephemeral_libvirt build host(s) could not be reached: {names}"

    @staticmethod
    def _error_category(unreachable: list[BuildHostProbeResult]) -> str:
        if unreachable and all(r.transport_error for r in unreachable):
            return _TRANSPORT_FAILURE
        return _CONFIGURATION_ERROR
