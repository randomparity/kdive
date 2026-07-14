"""Provider diagnostic check implementations (remote-libvirt and local-libvirt)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import StrEnum

from kdive.diagnostics.checks import (
    BASE_IMAGE_STAGING_ID,
    GDBSTUB_ACL_ID,
    MULTIARCH_GDB_ID,
    PROVIDER_TLS_ID,
    REACHABILITY_ID,
    Check,
    CheckResult,
    CheckStatus,
    Vantage,
)
from kdive.domain.errors import ErrorCategory

BASE_VOLUME_NOT_STAGED_FIX = (
    "base image volume is not staged on the remote host's storage pool; stage the "
    "operator-provided base image volume on the configured pool (ADR-0080), then retry"
)

MULTIARCH_GDB_MISSING_FIX = (
    "no gdb on this host can target a supported foreign architecture; install gdb-multiarch "
    "(Debian/Ubuntu) or a multiarch-capable gdb build so cross-arch debug sessions can attach"
)

_TRANSPORT_FAILURE = ErrorCategory.TRANSPORT_FAILURE
_CONFIGURATION_ERROR = ErrorCategory.CONFIGURATION_ERROR
_MISSING_DEPENDENCY = ErrorCategory.MISSING_DEPENDENCY


class TlsProbeOutcome(StrEnum):
    """The three observable outcomes of a provider TLS probe."""

    VALID = "valid"
    INVALID = "invalid"
    UNREACHABLE = "unreachable"


TlsProbe = Callable[[str], Awaitable[TlsProbeOutcome]]


class ProviderTlsCheck(Check):
    """Worker-vantage: the provider TLS chain validates against the configured CA."""

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


GdbstubAclProbe = Callable[[str, str], Awaitable[bool | None]]


class GdbstubAclCheck(Check):
    """Worker-vantage: the host ACL on ``config.gdb_addr`` admits the gdbstub port range."""

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
    """The three observable outcomes of a remote-libvirt reachability probe (ADR-0125)."""

    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"
    MISCONFIGURED = "misconfigured"


ReachabilityProbe = Callable[[], Awaitable[ReachabilityOutcome]]


class RemoteLibvirtReachabilityCheck(Check):
    """Server-vantage: the remote-libvirt host is libvirt-reachable."""

    def __init__(
        self, *, provider: str, probe: ReachabilityProbe, resource_id: str | None = None
    ) -> None:
        self._provider = provider
        self._probe = probe
        self._resource_id = resource_id

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
                resource_id=self._resource_id,
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
                resource_id=self._resource_id,
            )
        return CheckResult(
            check_id=self.id,
            status=CheckStatus.ERROR,
            detail="remote-libvirt reachability could not be probed; check the [[remote_libvirt]] "
            "URI, TLS cert refs, and systems.toml inventory",
            provider=self._provider,
            resource_id=self._resource_id,
            failure_category=_CONFIGURATION_ERROR,
        )


class BaseImageStagingOutcome(StrEnum):
    """The observable outcomes of a remote-libvirt base-image-staging probe (ADR-0150)."""

    STAGED = "staged"
    NOT_STAGED = "not_staged"
    UNREACHABLE = "unreachable"
    INDETERMINATE = "indeterminate"


BaseImageStagingProbe = Callable[[], Awaitable[BaseImageStagingOutcome]]


class BaseImageStagingCheck(Check):
    """Server-vantage: the operator-staged base-image volume is present on the host pool."""

    def __init__(
        self, *, provider: str, probe: BaseImageStagingProbe, resource_id: str | None = None
    ) -> None:
        self._provider = provider
        self._probe = probe
        self._resource_id = resource_id

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
                resource_id=self._resource_id,
            )
        if outcome is BaseImageStagingOutcome.NOT_STAGED:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.FAIL,
                detail="base image volume is not staged on the remote host's storage pool",
                fix=BASE_VOLUME_NOT_STAGED_FIX,
                provider=self._provider,
                failure_category=_CONFIGURATION_ERROR,
                resource_id=self._resource_id,
            )
        if outcome is BaseImageStagingOutcome.UNREACHABLE:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.ERROR,
                detail="remote-libvirt host unreachable; cannot verify base-image staging",
                provider=self._provider,
                failure_category=_TRANSPORT_FAILURE,
                resource_id=self._resource_id,
            )
        return CheckResult(
            check_id=self.id,
            status=CheckStatus.ERROR,
            detail="base-image staging could not be probed; check the [[remote_libvirt]] "
            "base_image / [[image]] staged volume, the storage pool, and the inventory",
            provider=self._provider,
            failure_category=_CONFIGURATION_ERROR,
            resource_id=self._resource_id,
        )


class MultiarchGdbOutcome(StrEnum):
    """The three observable outcomes of the multiarch-gdb prerequisite probe (ADR-0347)."""

    SUPPORTED = "supported"
    MISSING = "missing"
    UNDETERMINABLE = "undeterminable"


MultiarchGdbProbe = Callable[[], Awaitable[MultiarchGdbOutcome]]


class MultiarchGdbCheck(Check):
    """Worker-vantage: a multiarch-capable gdb can target every supported foreign arch (ADR-0347).

    Cross-arch debug sessions (a ppc64le guest on an x86_64 host) spawn a multiarch-capable gdb.
    This check runs where the worker spawns gdb and reports a missing prerequisite as a ``fail``
    with an actionable install hint, rather than letting a live attach fail opaquely later.
    """

    def __init__(self, *, provider: str, probe: MultiarchGdbProbe) -> None:
        self._provider = provider
        self._probe = probe

    @property
    def id(self) -> str:
        return MULTIARCH_GDB_ID

    @property
    def vantage(self) -> Vantage:
        return Vantage.WORKER

    async def run(self) -> CheckResult:
        outcome = await self._probe()
        if outcome is MultiarchGdbOutcome.SUPPORTED:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.PASS,
                detail="a multiarch-capable gdb targets every supported foreign architecture",
                provider=self._provider,
            )
        if outcome is MultiarchGdbOutcome.MISSING:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.FAIL,
                detail="no gdb on this host can target a supported foreign architecture",
                fix=MULTIARCH_GDB_MISSING_FIX,
                provider=self._provider,
                failure_category=_MISSING_DEPENDENCY,
            )
        return CheckResult(
            check_id=self.id,
            status=CheckStatus.ERROR,
            detail="could not run a candidate gdb to a multiarch verdict",
            provider=self._provider,
        )
