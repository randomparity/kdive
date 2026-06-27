"""Local kernel-source diagnostic check."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from kdive.diagnostics.checks import (
    LOCAL_KERNEL_SRC_ID,
    Check,
    CheckResult,
    CheckStatus,
    Vantage,
)
from kdive.domain.errors import ErrorCategory

LOCAL_KERNEL_SRC_FIX = (
    "stage a kernel source tree on the build worker and set KDIVE_KERNEL_SRC to its absolute "
    "path (resource://kdive/docs/operating/build-source-staging.md), or route builds to a "
    "registered git build host (build_hosts.register_ssh / build_hosts.register_ephemeral_libvirt)"
)


class WarmTreeSourceOutcome(StrEnum):
    """The three observable outcomes of a local warm-tree source probe."""

    USABLE = "usable"
    UNSET = "unset"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class WarmTreeSourceProbeResult:
    """A warm-tree source probe's verdict plus structured data to disclose (#845)."""

    outcome: WarmTreeSourceOutcome
    resolved_path: str | None = None
    head_commit: str | None = None
    branch: str | None = None


WarmTreeSourceProbe = Callable[[], Awaitable[WarmTreeSourceProbeResult]]


def _warm_tree_source_data(result: WarmTreeSourceProbeResult) -> dict[str, str]:
    data: dict[str, str] = {"vantage": Vantage.SERVER.value}
    if result.resolved_path is not None:
        data["resolved_path"] = result.resolved_path
    if result.head_commit is not None:
        data["head_commit"] = result.head_commit
    if result.branch is not None:
        data["branch"] = result.branch
    return data


async def _always_enabled() -> bool:
    return True


class LocalKernelSrcCheck(Check):
    """Server-vantage: the seeded local build host's warm-tree source is usable."""

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
                "(n/a - no local warm-tree lane to validate)",
            )
        result = await self._probe()
        if result.outcome is WarmTreeSourceOutcome.USABLE:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.PASS,
                detail="the server's KDIVE_KERNEL_SRC points at an existing absolute tree "
                "(server vantage - not authoritative for a split-deployment build worker, "
                "whose env may differ; ADR-0163)",
                data=_warm_tree_source_data(result),
            )
        if result.outcome is WarmTreeSourceOutcome.UNSET:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.FAIL,
                detail="the server's KDIVE_KERNEL_SRC is unset, so the local warm-tree build lane "
                "has no kernel source and every local warm-tree build fails (server vantage; "
                "a split-deployment build worker may also need it set - ADR-0163)",
                fix=LOCAL_KERNEL_SRC_FIX,
                failure_category=ErrorCategory.CONFIGURATION_ERROR,
            )
        return CheckResult(
            check_id=self.id,
            status=CheckStatus.FAIL,
            detail="the server's KDIVE_KERNEL_SRC is set but is not an absolute path to an "
            "existing kernel source tree, so every local warm-tree build fails (server vantage; "
            "ADR-0163)",
            fix=LOCAL_KERNEL_SRC_FIX,
            failure_category=ErrorCategory.CONFIGURATION_ERROR,
        )
