"""Secret-reference diagnostic check."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Sequence

from kdive.diagnostics.checks import SECRET_REF_ID, Check, CheckResult, CheckStatus, Vantage

_log = logging.getLogger(__name__)

SecretResolve = Callable[[str], object]


def _redact_exception_args(exc: Exception) -> None:
    """Remove ref-bearing exception args before traceback logging formats the exception."""
    with contextlib.suppress(Exception):
        exc.args = (f"{type(exc).__name__} while resolving configured secret ref",)


class SecretRefCheck(Check):
    """Server-vantage: every configured secret ref resolves in the backend (ADR-0091 §2)."""

    def __init__(
        self,
        *,
        refs: Sequence[tuple[str, bool]],
        resolve: SecretResolve,
        backend_unreachable: type[Exception] | tuple[type[Exception], ...] = (),
    ) -> None:
        """Build the check.

        Args:
            refs: ``(ref, is_platform)`` pairs for every configured secret ref.
            resolve: Resolves one ref, raising on a ref that does not resolve.
            backend_unreachable: Exception type(s) signalling the backend itself is
                unreachable, distinct from a per-ref miss.
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
