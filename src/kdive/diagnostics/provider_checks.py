"""Provider diagnostic check implementations (remote-libvirt and local-libvirt)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from kdive.diagnostics.checks import (
    BASE_IMAGE_STAGING_ID,
    GDBSTUB_ACL_ID,
    GUEST_ARCH_ACCEL_ID,
    MULTIARCH_GDB_ID,
    PROVIDER_TLS_ID,
    PSERIES_FADUMP_ID,
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

PSERIES_FADUMP_UNSUPPORTED_FIX = (
    "this host's qemu-system-ppc64 predates QEMU 10.2, which added the pseries "
    "ibm,configure-kernel-dump RTAS fadump implements; upgrade QEMU to >= 10.2 to provision "
    "fadump systems here, or validate fadump on native POWER. kdump is unaffected."
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


class PseriesFadumpOutcome(StrEnum):
    """The three observable outcomes of the host pseries-fadump prerequisite probe (ADR-0349)."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    NOT_APPLICABLE = "not_applicable"


PseriesFadumpProbe = Callable[[], Awaitable[PseriesFadumpOutcome]]


class PseriesFadumpCheck(Check):
    """Worker-vantage: the host QEMU implements pseries firmware-assisted dump (ADR-0349).

    fadump needs QEMU >= 10.2 (the ``ibm,configure-kernel-dump`` RTAS). This check reports a
    host qemu-system-ppc64 below that floor as a ``fail`` with an upgrade hint, so a fadump
    provision that admission would reject is legible at doctor time rather than only at
    provision. A host with no qemu-system-ppc64 cannot run ppc64le guests at all, so fadump is
    ``not_applicable`` there and the check passes.
    """

    def __init__(self, *, provider: str, probe: PseriesFadumpProbe) -> None:
        self._provider = provider
        self._probe = probe

    @property
    def id(self) -> str:
        return PSERIES_FADUMP_ID

    @property
    def vantage(self) -> Vantage:
        return Vantage.WORKER

    async def run(self) -> CheckResult:
        outcome = await self._probe()
        if outcome is PseriesFadumpOutcome.SUPPORTED:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.PASS,
                detail="the host qemu-system-ppc64 implements pseries fadump (QEMU >= 10.2)",
                provider=self._provider,
            )
        if outcome is PseriesFadumpOutcome.NOT_APPLICABLE:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.PASS,
                detail="no qemu-system-ppc64 on this host; pseries fadump is not applicable",
                provider=self._provider,
            )
        return CheckResult(
            check_id=self.id,
            status=CheckStatus.FAIL,
            detail="the host qemu-system-ppc64 predates QEMU 10.2 and does not implement fadump",
            fix=PSERIES_FADUMP_UNSUPPORTED_FIX,
            provider=self._provider,
            failure_category=_MISSING_DEPENDENCY,
        )


@dataclass(frozen=True, slots=True)
class GuestArchAccelReport:
    """The per-arch guest-accelerator facts observed on the worker host (ADR-0352).

    Attributes:
        accel_by_arch: ``{arch: "kvm"|"tcg"}`` for every supported arch whose qemu
            emulator is present on the worker's PATH (arch-sorted at construction). A guest
            arch is ``kvm`` only when it is the host's native arch and the URI-selected KVM
            signal holds, else ``tcg``.
        native_arch: The worker host's own architecture (``platform.machine()``).
        native_supported: Whether ``native_arch`` is an arch kdive can provision.
        native_emulator_present: Whether the qemu emulator for ``native_arch`` is on PATH.
        native_qemu_binary: The qemu system-emulator binary name for ``native_arch``, or
            ``None`` when the host arch is unsupported (so no native binary is expected).
        target_is_local: Whether the configured libvirt URI runs guests on this worker (a local
            hypervisor). ``False`` for a transport URI (``qemu+ssh://…``) whose guests run on
            another host, where the worker's PATH/``/dev/kvm`` do not describe the target — so the
            native-emulator FAIL is suppressed and the accel map is scoped as worker-local.
    """

    accel_by_arch: Mapping[str, str]
    native_arch: str
    native_supported: bool
    native_emulator_present: bool
    native_qemu_binary: str | None
    target_is_local: bool = True


GuestArchAccelProbe = Callable[[], Awaitable[GuestArchAccelReport]]


def _accel_phrase(arch: str, accel: str) -> str:
    return f"{arch} (KVM native)" if accel == "kvm" else f"{arch} (TCG-only)"


def _describe_accel(report: GuestArchAccelReport) -> str:
    """Render the human-readable accel summary, flagging a native arch that fell to TCG."""
    if not report.accel_by_arch:
        detail = "no qemu system emulator found on PATH; no guest arch is schedulable here"
    else:
        parts = [_accel_phrase(a, report.accel_by_arch[a]) for a in sorted(report.accel_by_arch)]
        detail = "schedulable guest arches: " + ", ".join(parts)
        if report.accel_by_arch.get(report.native_arch) == "tcg":
            detail = (
                f"native arch {report.native_arch} is TCG-only (host KVM unavailable); " + detail
            )
    if not report.target_is_local:
        detail += (
            " (probing the local worker host; the configured libvirt URI targets a remote host, "
            "so guests may run elsewhere)"
        )
    return detail


class GuestArchAccelCheck(Check):
    """Worker-vantage: which guest arches are schedulable here, and at what accelerator (ADR-0352).

    Reports the KVM-vs-TCG accel map (in ``data``, so it reaches ``doctor --json``) and
    FAILs on exactly one condition — the host cannot schedule even its own native arch (its
    qemu emulator is absent). Native-arch-under-TCG is a *degraded, not broken* state
    (still provisions, only slower), so it PASSes but is flagged in ``detail``; a foreign
    arch's absence never fails (cross-arch is optional).
    """

    def __init__(self, *, provider: str, probe: GuestArchAccelProbe) -> None:
        self._provider = provider
        self._probe = probe

    @property
    def id(self) -> str:
        return GUEST_ARCH_ACCEL_ID

    @property
    def vantage(self) -> Vantage:
        return Vantage.WORKER

    async def run(self) -> CheckResult:
        report = await self._probe()
        # The accel map describes the worker host. For a remote/transport URI that is the wrong
        # host, so emit a machine-readable marker instead of a confidently-wrong per-arch map (the
        # human detail is scoped too); a local target carries the real accel map.
        data = dict(report.accel_by_arch) or None
        if not report.target_is_local:
            data = {"target_is_local": "false"}
        # Only gate native schedulability for a local target: for a remote/transport URI the
        # emulator lives on another host this worker-vantage probe cannot see, so a missing local
        # emulator is not a real schedulability failure (never a confident wrong FAIL).
        if (
            report.native_supported
            and not report.native_emulator_present
            and report.target_is_local
        ):
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.FAIL,
                detail=(
                    f"host cannot schedule its own native arch {report.native_arch}: "
                    f"{report.native_qemu_binary} not found on PATH"
                ),
                fix=(
                    f"{report.native_qemu_binary} not found on PATH; install it via your "
                    "distribution package manager (see scripts/check-setup-deps.sh for "
                    "per-distro hints)"
                ),
                provider=self._provider,
                failure_category=_MISSING_DEPENDENCY,
                data=data,
            )
        return CheckResult(
            check_id=self.id,
            status=CheckStatus.PASS,
            detail=_describe_accel(report),
            provider=self._provider,
            data=data,
        )
