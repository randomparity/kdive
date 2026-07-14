"""The shared diagnostic ``Check`` framework and stable check ids (ADR-0091 §2).

A ``Check`` is an ``id``, a ``vantage``, and an async ``run() -> CheckResult``, where
``CheckResult.status`` is **three-state**: ``pass`` (the contract holds), ``fail`` (the
contract is violated and ``fix`` names the exact remediation), and ``error`` (the check
could not be run to a verdict, so ``detail`` says what blocked it).

Every check runs through :func:`run_check`, which bounds it by a per-check timeout and
converts any unexpected exception into ``error`` so one broken probe cannot wedge the
aggregating service.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from kdive.domain.errors import ErrorCategory

SECRET_REF_ID = "secret_ref"
PROVIDER_TLS_ID = "provider_tls"
GDBSTUB_ACL_ID = "gdbstub_acl"
REACHABILITY_ID = "remote_libvirt_reachability"
BASE_IMAGE_STAGING_ID = "remote_libvirt_base_image_staging"
MULTIARCH_GDB_ID = "multiarch_gdb"

_log = logging.getLogger(__name__)


class CheckStatus(StrEnum):
    """The three-state verdict of a single check (ADR-0091 §2)."""

    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"


class Vantage(StrEnum):
    """Where a check must run from to observe the contract it probes."""

    SERVER = "server"
    WORKER = "worker"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One check's three-state verdict (ADR-0091 §2).

    Args:
        check_id: The stable id of the check that produced this result.
        status: The three-state verdict.
        detail: On ``fail``, what contract is violated; on ``error``, what blocked the
            check; on ``pass``, a short confirmation.
        fix: The exact remediation. Mandatory on ``fail`` and forbidden otherwise.
        provider: The provider this result pertains to, or ``None`` for provider-independent
            checks.
        failure_category: The :class:`ErrorCategory` for why the contract was violated or
            why the check could not run. ``None`` on ``pass``.
        resource_id: The registered resource this result pertains to, when the check is
            scoped to a concrete resource.
        data: Structured, machine-readable non-secret fields surfaced with the verdict.
    """

    check_id: str
    status: CheckStatus
    detail: str
    fix: str | None = None
    provider: str | None = None
    failure_category: ErrorCategory | None = None
    resource_id: str | None = None
    data: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if self.failure_category is not None and not isinstance(
            self.failure_category, ErrorCategory
        ):
            raise TypeError(
                f"{self.check_id}: failure_category must be an ErrorCategory "
                f"(got {type(self.failure_category).__name__})"
            )
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
        """The stable check id."""

    @property
    @abstractmethod
    def vantage(self) -> Vantage:
        """Where this check must run from."""

    @abstractmethod
    async def run(self) -> CheckResult:
        """Probe the contract and return a three-state result."""


async def run_check(check: Check, *, timeout: float) -> CheckResult:
    """Run ``check`` bounded by ``timeout``; map timeout or unexpected error to ``error``."""
    try:
        async with asyncio.timeout(timeout):
            return await check.run()
    except TimeoutError:
        return CheckResult(
            check_id=check.id,
            status=CheckStatus.ERROR,
            detail=f"check did not respond within {timeout:g}s",
            failure_category=ErrorCategory.TRANSPORT_FAILURE,
        )
    except Exception as exc:  # noqa: BLE001 - a leaked error must not wedge the service
        _log.error("diagnostic check %s raised unexpectedly: %s", check.id, exc, exc_info=True)
        return CheckResult(
            check_id=check.id,
            status=CheckStatus.ERROR,
            detail="check could not be run to a verdict (unexpected error)",
            failure_category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        )
