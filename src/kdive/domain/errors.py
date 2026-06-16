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
            terminal: Whether the job raising this error must dead-letter at once rather
                than requeue, irrespective of category. Set when a retry cannot succeed
                because the failure already drove the target to a terminal state (e.g. a
                provision failure left the System ``failed``), so requeuing would only mask
                the failure as a success on the next attempt.
        """
        super().__init__(message)
        self.category = category
        self.details = details or {}
        self.terminal = terminal
