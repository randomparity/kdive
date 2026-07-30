"""Stable error taxonomy and the typed failure carrier (ADR-0001).

The PoC's stable :class:`ErrorCategory` is reused so failure strings stay
comparable across the rewrite. This module carries the categories current tool and
provider seams can emit, plus the distributed categories introduced by the service
architecture and the object-lookup categories (``not_found``/``conflict``, ADR-0097).
The PoC's ``test_failure`` is intentionally absent because there is no test plane
emitting it.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCategory(StrEnum):
    """The closed set of failure categories a tool may report.

    Values are stable wire strings — handlers pick the most specific category and
    never invent new strings (``m0-walking-skeleton.md``).
    """

    # Reused from the PoC taxonomy.
    CONFIGURATION_ERROR = "configuration_error"
    MISSING_DEPENDENCY = "missing_dependency"
    BUILD_FAILURE = "build_failure"
    BOOT_TIMEOUT = "boot_timeout"
    READINESS_FAILURE = "readiness_failure"
    DEBUG_ATTACH_FAILURE = "debug_attach_failure"
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"
    STALE_HANDLE = "stale_handle"
    TRANSPORT_CONFLICT = "transport_conflict"
    NOT_IMPLEMENTED = "not_implemented"

    # Object-lookup categories (#338, ADR-0097). A syntactically valid id that resolves to no
    # visible row is ``not_found`` (distinct from a malformed id, which stays
    # ``configuration_error``). ``conflict`` is reserved for a uniqueness/state conflict and is
    # defined-but-unemitted until a concrete state-conflict seam needs it.
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"

    # Distributed categories for the async worker / provider seams.
    ALLOCATION_DENIED = "allocation_denied"
    QUOTA_EXCEEDED = "quota_exceeded"
    LEASE_EXPIRED = "lease_expired"
    QUEUE_TIMEOUT = "queue_timeout"
    PROVISIONING_FAILURE = "provisioning_failure"
    INSTALL_FAILURE = "install_failure"
    TRANSPORT_FAILURE = "transport_failure"
    CONTROL_FAILURE = "control_failure"
    AUTHORIZATION_DENIED = "authorization_denied"
    # Build-host scheduling (#342): all registered hosts are at capacity; distinct from
    # `quota_exceeded` (per-project concurrency cap) and `allocation_denied` (over-budget).
    CAPACITY_EXHAUSTED = "capacity_exhausted"

    # Debug-plane symbol resolution (#1013, ADR-0307). `debug.resolve_symbol` on a symbol gdb cannot
    # resolve to an address — inlined / optimized away, or an addressless enum/macro constant — is a
    # clean not-found, distinct from `debug_attach_failure` (the session is fine) and non-retryable
    # (the symbol will not appear on a bare re-invocation).
    SYMBOL_NOT_FOUND = "symbol_not_found"

    # Snapshot-restore limbo (#1560, ADR-0513). A `restoring` System whose restore job can never
    # run again — the worker died mid-revert, or the job dead-lettered / was canceled — is driven
    # to `failed` by the reconciler with no exception and no job to attribute. The guest's disk
    # state is indeterminate, which is a different operator response from `infrastructure_failure`
    # (an unclassified fault in the layer below, and retryable).
    RESTORE_INCOMPLETE = "restore_incomplete"


# Categories whose human-readable reason must never reach a client (ADR-0123): a denial or a
# by-id lookup miss carries a fixed constant so no raise site — even one whose message embeds a
# named project or object id — can leak resource existence through the envelope's `detail`.
_SUPPRESSED_DETAIL: dict[ErrorCategory, str] = {
    ErrorCategory.AUTHORIZATION_DENIED: "access denied",
    ErrorCategory.NOT_FOUND: "not found",
}


def suppressed_detail(category: ErrorCategory, raw: str | None) -> str | None:
    """Resolve the surfaced ``detail`` for ``category`` under the no-leak seam rule (ADR-0123).

    For a suppressed category the fixed constant wins and ``raw`` is ignored, so no raise site can
    leak a resource name through ``detail``. For every other (diagnostic) category ``raw`` — the
    ``CategorizedError`` message — passes through unchanged.

    Args:
        category: The failure category being enveloped.
        raw: The candidate detail (typically ``str(exc)``); may be ``None``.

    Returns:
        The fixed constant for a suppressed category, else ``raw``.
    """
    return _SUPPRESSED_DETAIL.get(category, raw)


# Retryability is a pure function of the failure category (ADR-0118): a bare re-invocation may
# succeed once a transient condition clears, with no caller change. Exhaustive over ErrorCategory;
# the bias is non-retryable when transience is ambiguous, since the flag exists to stop a permanent
# failure being hammered (#430).
#
# ONE table serves BOTH retry seams (ADR-0483): the `retryable` boolean on the MCP response
# envelope, and the job queue's choice between dead-lettering a failed attempt and re-dispatching
# it. It lives here, beside ErrorCategory, because `kdive.jobs` must not import `kdive.mcp` — a
# second copy in the queue would be free to drift from the one agents are told to trust.
RETRYABLE_BY_CATEGORY: dict[ErrorCategory, bool] = {
    ErrorCategory.INFRASTRUCTURE_FAILURE: True,
    ErrorCategory.PROVISIONING_FAILURE: True,
    ErrorCategory.BOOT_TIMEOUT: True,
    ErrorCategory.READINESS_FAILURE: True,
    ErrorCategory.TRANSPORT_FAILURE: True,
    ErrorCategory.TRANSPORT_CONFLICT: True,
    ErrorCategory.DEBUG_ATTACH_FAILURE: True,
    ErrorCategory.CONTROL_FAILURE: True,
    ErrorCategory.CAPACITY_EXHAUSTED: True,
    ErrorCategory.QUEUE_TIMEOUT: True,
    ErrorCategory.CONFIGURATION_ERROR: False,
    ErrorCategory.MISSING_DEPENDENCY: False,
    ErrorCategory.BUILD_FAILURE: False,
    ErrorCategory.INSTALL_FAILURE: False,
    ErrorCategory.STALE_HANDLE: False,
    ErrorCategory.LEASE_EXPIRED: False,
    ErrorCategory.NOT_IMPLEMENTED: False,
    ErrorCategory.NOT_FOUND: False,
    ErrorCategory.SYMBOL_NOT_FOUND: False,
    ErrorCategory.RESTORE_INCOMPLETE: False,
    ErrorCategory.CONFLICT: False,
    ErrorCategory.AUTHORIZATION_DENIED: False,
    ErrorCategory.QUOTA_EXCEEDED: False,
    ErrorCategory.ALLOCATION_DENIED: False,
}


def retryable_category(category: ErrorCategory) -> bool:
    """Return whether a bare retry of a failure in ``category`` can succeed (ADR-0118).

    Args:
        category: The taxonomy category the failure was classified as.

    Returns:
        ``True`` when the condition is transient, so re-running the identical request may
        succeed; ``False`` when it is permanent until something outside the request changes.
    """
    return RETRYABLE_BY_CATEGORY[category]


class CategorizedError(Exception):
    """An error carrying the :class:`ErrorCategory` a failure response needs.

    Raised by domain and provider code so a handler maps any failure onto a
    typed failure response without per-exception special-casing.
    """

    def __init__(
        self,
        message: str,
        *,
        category: ErrorCategory,
        details: dict[str, object] | None = None,
        terminal: bool = False,
    ) -> None:
        """Build a categorized error.

        Args:
            message: Human-readable failure description.
            category: The taxonomy category this failure maps to.
            details: Optional structured context (must be free of secret material;
                it may be surfaced in responses and logs).
            terminal: Escalate a job failure in an otherwise **retryable** category to an
                immediate dead-letter. Set when a retry cannot succeed because the failure
                already drove the target to a terminal state (e.g. a provision failure left
                the System ``failed``), so requeuing would only mask the failure as a success
                on the next attempt. A category :data:`RETRYABLE_BY_CATEGORY` already calls
                non-retryable dead-letters on its own (ADR-0483) and needs no flag.
        """
        super().__init__(message)
        self.category = category
        self.details = details or {}
        self.terminal = terminal
