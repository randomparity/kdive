"""Shared MCP tool-boundary helpers."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from kdive.domain.errors import ErrorCategory
from kdive.domain.operations.jobs import Job
from kdive.jobs.context import authorizing, context_from_job
from kdive.mcp.responses import ResponseDataInput, ToolResponse, current_status_data
from kdive.serialization import JsonValue

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200


class ConfigErrorReason(StrEnum):
    """Closed vocabulary of machine-readable `configuration_error` reasons (ADR-0174).

    Surfaced under ``data.reason`` so a black-box MCP client can self-correct a
    parse/validation failure. A closed enum (not bare literals) so a call site can only emit a
    known token and a typo is a type error rather than a silently-shipped string.
    """

    INVALID_UUID = "invalid_uuid"
    INVALID_STATE = "invalid_state"
    INVALID_TRANSPORT = "invalid_transport"
    INVALID_EXTERNAL_REF = "invalid_external_ref"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_TIMEOUT = "invalid_timeout"
    INVALID_TEXT = "invalid_text"
    INVALID_PCIE_MATCH = "invalid_pcie_match"


def as_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def clamp_list_limit(limit: int) -> int:
    return max(1, min(limit, MAX_LIST_LIMIT))


def config_error(
    object_id: str, *, detail: str | None = None, data: ResponseDataInput | None = None
) -> ToolResponse:
    return ToolResponse.failure(
        object_id, ErrorCategory.CONFIGURATION_ERROR, detail=detail, data=data or {}
    )


def config_error_reason(
    object_id: str,
    reason: ConfigErrorReason,
    *,
    accepted_values: list[str] | None = None,
    detail: str | None = None,
) -> ToolResponse:
    """Build a ``configuration_error`` carrying a machine-readable reason (ADR-0174).

    ``reason`` lands in ``data.reason``; a finite valid set lands in ``data.accepted_values``
    (sorted for a stable wire order). ``detail`` is a fixed-template human one-liner — it must
    not interpolate secrets, secret-ref paths, internal hostnames, object-store keys, or a
    resource name the caller did not supply (ADR-0123). ``configuration_error`` is not a
    suppressed category, so both ``detail`` and ``data`` pass through unchanged.
    """
    data: dict[str, JsonValue] = {"reason": reason.value}
    if accepted_values is not None:
        data["accepted_values"] = [value for value in sorted(accepted_values)]
    return ToolResponse.failure(
        object_id, ErrorCategory.CONFIGURATION_ERROR, detail=detail, data=data
    )


def not_found(object_id: str, *, data: ResponseDataInput | None = None) -> ToolResponse:
    """Build a ``not_found`` failure envelope for a valid-but-absent object id (ADR-0097).

    Distinct from :func:`config_error`: a malformed id is a parse failure
    (``configuration_error``); a syntactically valid id with no visible row is ``not_found``.
    An id in an ungranted project resolves here too, so the envelope is byte-identical to a
    genuinely-absent one (no membership leak).
    """
    return ToolResponse.failure(object_id, ErrorCategory.NOT_FOUND, data=data or {})


def stale_handle(object_id: str, *, current_status: str) -> ToolResponse:
    return ToolResponse.failure(
        object_id, ErrorCategory.STALE_HANDLE, data=current_status_data(current_status)
    )


def authz_denied(object_id: str, missing_checks: list[str]) -> ToolResponse:
    """Build an ``authorization_denied`` envelope naming the failed gate checks (ADR-0129).

    ``missing_checks`` is the destructive-op gate's closed enum of policy-check tokens
    (``admin_role``/``operator_role``, ``profile_opt_in``) — never a resource identifier — so
    it is safe to surface in ``data`` under the no-leak seam (ADR-0123), which suppresses
    ``detail`` only, not ``data``.
    """
    checks: list[JsonValue] = list(missing_checks)
    return ToolResponse.failure(
        object_id, ErrorCategory.AUTHORIZATION_DENIED, data={"missing_checks": checks}
    )


def job_envelope(job: Job, object_key: str, object_id: UUID) -> ToolResponse:
    base = ToolResponse.from_job(job)
    return base.model_copy(update={"data": {**base.data, object_key: str(object_id)}})


__all__ = [
    "DEFAULT_LIST_LIMIT",
    "MAX_LIST_LIMIT",
    "ConfigErrorReason",
    "as_uuid",
    "authorizing",
    "authz_denied",
    "clamp_list_limit",
    "config_error",
    "config_error_reason",
    "context_from_job",
    "job_envelope",
    "not_found",
    "stale_handle",
]
