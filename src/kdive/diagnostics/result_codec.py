"""Inline (de)serialization of worker-vantage CheckResults carried in a job's result_ref (ADR-0164).

The diagnostics worker job returns its CheckResults as a compact JSON string inline in
``result_ref`` (the verdict is small, non-secret, and read only by the dispatcher). The dispatcher
reconstructs each result through :class:`CheckResult`, re-running its invariants, and accepts only
the registered worker-vantage check ids. A payload that cannot be parsed at all (empty, invalid
JSON, no ``results`` list) raises :class:`ResultCodecError` for the whole batch, since there is no
per-item boundary to attribute it to. A single rejected item (an unexpected id, a bad enum value,
an invariant violation) instead degrades to an ``error`` :class:`CheckResult` for that item alone,
so one bad id cannot poison its recognised siblings.
"""

from __future__ import annotations

import json
from typing import Any

from kdive.diagnostics.checks import (
    DEPMOD_TOOLCHAIN_ID,
    GDBSTUB_ACL_ID,
    GUEST_ARCH_ACCEL_ID,
    MULTIARCH_GDB_ID,
    PROVIDER_TLS_ID,
    PSERIES_FADUMP_ID,
    CheckResult,
    CheckStatus,
)
from kdive.domain.errors import ErrorCategory

_ALLOWED_IDS = frozenset(
    {
        PROVIDER_TLS_ID,
        GDBSTUB_ACL_ID,
        MULTIARCH_GDB_ID,
        PSERIES_FADUMP_ID,
        GUEST_ARCH_ACCEL_ID,
        DEPMOD_TOOLCHAIN_ID,
    }
)


class ResultCodecError(ValueError):
    """The inline worker result is malformed, empty, or carries an unexpected check id."""


def serialize_results(results: list[CheckResult]) -> str:
    """Serialize worker-vantage CheckResults to a compact JSON string for inline transport."""
    return json.dumps(
        {
            "results": [
                {
                    "check_id": r.check_id,
                    "status": r.status.value,
                    "detail": r.detail,
                    "fix": r.fix,
                    "provider": r.provider,
                    "failure_category": (
                        r.failure_category.value if r.failure_category is not None else None
                    ),
                    "resource_id": r.resource_id,
                }
                for r in results
            ]
        },
        separators=(",", ":"),
    )


def deserialize_results(raw: str | None) -> list[CheckResult]:
    """Parse inline worker results, degrading each rejected item to its own ``error`` result.

    Raises ResultCodecError only when the payload itself cannot be parsed (empty, invalid JSON,
    no ``results`` list) — there is no per-item boundary to attribute that to.
    """
    if not raw:
        raise ResultCodecError("empty diagnostics result")
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ResultCodecError(f"diagnostics result is not valid JSON: {exc}") from exc
    items = doc.get("results") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        raise ResultCodecError("diagnostics result has no 'results' list")
    return [_reconstruct_or_error(item) for item in items]


def _reconstruct_or_error(item: Any) -> CheckResult:
    """Reconstruct one item, mapping a rejected item to an ``error`` result for its own check id."""
    try:
        return _reconstruct(item)
    except ResultCodecError as exc:
        check_id = item.get("check_id") if isinstance(item, dict) else None
        return CheckResult(
            check_id=check_id if isinstance(check_id, str) else "unknown",
            status=CheckStatus.ERROR,
            detail=str(exc),
            failure_category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        )


def _reconstruct(item: Any) -> CheckResult:
    if not isinstance(item, dict):
        raise ResultCodecError("diagnostics result item is not an object")
    check_id = item.get("check_id")
    if check_id not in _ALLOWED_IDS:
        raise ResultCodecError(f"unexpected worker-vantage check id {check_id!r}")
    try:
        return CheckResult(
            check_id=check_id,
            status=CheckStatus(item["status"]),
            detail=item["detail"],
            fix=item.get("fix"),
            provider=item.get("provider"),
            failure_category=(
                ErrorCategory(item["failure_category"])
                if item.get("failure_category") is not None
                else None
            ),
            resource_id=item.get("resource_id"),
        )
    except (KeyError, ValueError) as exc:  # missing field, bad enum, or invariant violation
        raise ResultCodecError(f"invalid diagnostics result item: {exc}") from exc
