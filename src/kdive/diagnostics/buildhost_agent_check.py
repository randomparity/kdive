"""Ephemeral-libvirt buildhost agent diagnostic check."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from kdive.diagnostics.checks import (
    BUILDHOST_AGENT_ID,
    Check,
    CheckResult,
    CheckStatus,
    Vantage,
)
from kdive.domain.errors import ErrorCategory

BUILDHOST_AGENT_FIX = (
    "an ephemeral_libvirt build host's throwaway builder boots but its qemu-guest-agent never "
    "becomes usable; rebuild or repair the operator-staged base build image so its guest agent "
    "starts (resource://kdive/docs/operating/build-source-staging.md), then re-run doctor "
    "--with-buildhost-agent"
)


class BuildHostAgentOutcome(StrEnum):
    """The per-host observable outcomes of the ephemeral build-host agent probe."""

    AGENT_READY = "agent_ready"
    AGENT_UNREACHABLE = "agent_unreachable"
    HOST_UNREACHABLE = "host_unreachable"


@dataclass(frozen=True, slots=True)
class BuildHostProbeResult:
    """One probed host's outcome."""

    host_name: str
    outcome: BuildHostAgentOutcome
    transport_error: bool = False


BuildHostAgentProbe = Callable[[], Awaitable[list[BuildHostProbeResult]]]


class EphemeralLibvirtBuildHostAgentCheck(Check):
    """Server-vantage: every ephemeral_libvirt build host's builder reaches its guest agent."""

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
                failure_category=ErrorCategory.CONFIGURATION_ERROR,
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
    def _error_category(unreachable: list[BuildHostProbeResult]) -> ErrorCategory:
        if unreachable and all(r.transport_error for r in unreachable):
            return ErrorCategory.TRANSPORT_FAILURE
        return ErrorCategory.CONFIGURATION_ERROR
