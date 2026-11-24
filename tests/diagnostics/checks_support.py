"""Deterministic diagnostic checks shared by service and dispatch proofs."""

from __future__ import annotations

import asyncio

from kdive.diagnostics.checks import Check, CheckResult, Vantage


class FixedCheck(Check):
    def __init__(self, result: CheckResult, vantage: Vantage = Vantage.SERVER) -> None:
        self._result = result
        self._vantage = vantage

    @property
    def id(self) -> str:
        return self._result.check_id

    @property
    def vantage(self) -> Vantage:
        return self._vantage

    async def run(self) -> CheckResult:
        return self._result


class SlowCheck(FixedCheck):
    def __init__(self, result: CheckResult, *, delay: float) -> None:
        super().__init__(result)
        self._delay = delay

    async def run(self) -> CheckResult:
        await asyncio.sleep(self._delay)
        return self._result
