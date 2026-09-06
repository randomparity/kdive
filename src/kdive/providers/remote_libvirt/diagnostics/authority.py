"""Authentication-only readiness without network identity or peer output (ADR-0606)."""

import asyncio
from collections.abc import Callable

from kdive.diagnostics.checks import Check, CheckResult, CheckStatus, Vantage
from kdive.domain.errors import ErrorCategory
from kdive.providers.ports.authority import AuthorityRequestSender

AUTHORITY_READINESS_ID = "provider_authority"
_HEALTH_BUDGET_SECONDS = 5.0


class AuthorityReadinessCheck(Check):
    """Construct and authenticate the selected route within the worker diagnostic budget."""

    def __init__(self, build: Callable[[], AuthorityRequestSender | None]) -> None:
        self._build = build

    @property
    def id(self) -> str:
        return AUTHORITY_READINESS_ID

    @property
    def vantage(self) -> Vantage:
        return Vantage.WORKER

    async def run(self) -> CheckResult:
        readiness = "unavailable"
        try:
            deadline = asyncio.get_running_loop().time() + _HEALTH_BUDGET_SECONDS
            async with asyncio.timeout_at(deadline):
                sender = self._build()
                if sender is None:
                    readiness = "unadvertised"
                else:
                    await sender.health(deadline=deadline)
                    readiness = "ready"
        except Exception:  # noqa: BLE001 -- no identity, secret ref, or peer diagnostic escapes
            pass
        ready = readiness == "ready"
        return CheckResult(
            check_id=self.id,
            status=CheckStatus.PASS if ready else CheckStatus.ERROR,
            detail=f"authority: {readiness}",
            provider="remote-libvirt",
            failure_category=None if ready else ErrorCategory.READINESS_FAILURE,
            data={"readiness": readiness},
        )
