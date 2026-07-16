"""Shared MCP tool-boundary helpers."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Sequence
from datetime import datetime
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
    INVALID_CURSOR = "invalid_cursor"
    INVALID_VERSION = "invalid_version"
    KDUMP_INCAPABLE = "kdump_incapable"


_MAX_ECHOED_ID = 64
"""Cap on a caller-supplied id echoed into ``detail`` (ADR-0166/0174 echo rule)."""


def as_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def _short_id(value: str) -> str:
    """Bound a caller-supplied id for safe echo into ``detail`` (ADR-0166/0174).

    A malformed id is unbounded caller input; echoing it whole would let a hostile caller
    reflect an arbitrarily large string into the response. Truncate to ``_MAX_ECHOED_ID`` with
    an ellipsis marker so the surfaced value stays short and bounded.
    """
    if len(value) <= _MAX_ECHOED_ID:
        return value
    return f"{value[:_MAX_ECHOED_ID]}…"


def invalid_uuid_error(field: str, raw_id: str) -> ToolResponse:
    """A ``configuration_error`` naming a malformed ``field`` id (ADR-0174).

    The echoed id is bounded (``_short_id``) so an oversized malformed id cannot blow up
    ``detail``. The full (unbounded) value remains the envelope ``object_id`` as before.
    """
    return config_error_reason(
        raw_id,
        ConfigErrorReason.INVALID_UUID,
        detail=f"{field} {_short_id(raw_id)!r} is not a valid UUID",
    )


def clamp_list_limit(limit: int) -> int:
    return max(1, min(limit, MAX_LIST_LIMIT))


class InvalidCursor(Exception):
    """A list `cursor` is malformed, minted by a different tool, or the wrong shape (ADR-0192).

    Raised by :func:`decode_cursor`; a list handler maps it to an ``invalid_cursor``
    ``configuration_error`` (:func:`invalid_cursor_error`). Never a silent first-page
    fallback — a silent fallback would trap an agent re-reading page one forever.
    """


def encode_cursor(tool_tag: str, key_parts: Sequence[str]) -> str:
    """Encode a list page's continuation cursor as an opaque base64url token (ADR-0192).

    The token wraps the producing tool's tag plus the last returned row's sort key (each
    part stringified) so a cursor minted by one list is rejected by another. It is not a
    security token: every page re-applies the same project/role ``WHERE`` clause, so the
    cursor only expresses a sort-key boundary.
    """
    payload = json.dumps({"t": tool_tag, "k": list(key_parts)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_cursor(tool_tag: str, cursor: str, *, arity: int) -> list[str]:
    """Decode an opaque list cursor, validating its tag and shape (ADR-0192).

    Returns the ``arity`` sort-key parts. Raises :class:`InvalidCursor` when the token is
    not valid base64url, not the expected JSON object, carries a different tool's tag, or
    whose ``k`` is not a list of exactly ``arity`` strings.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        obj = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeError) as exc:
        raise InvalidCursor("cursor is not a valid token") from exc
    if not isinstance(obj, dict) or obj.get("t") != tool_tag:
        raise InvalidCursor("cursor was not minted by this tool")
    key = obj.get("k")
    if not isinstance(key, list) or len(key) != arity:
        raise InvalidCursor("cursor has the wrong shape")
    if not all(isinstance(part, str) for part in key):
        raise InvalidCursor("cursor key parts must be strings")
    return [part for part in key if isinstance(part, str)]


def paginate[T](rows: list[T], limit: int) -> tuple[list[T], bool]:
    """Split a ``limit + 1`` fetch into the kept page and a truncation flag (ADR-0192).

    A handler fetches one row past ``limit``; this drops the extra and reports
    ``truncated=True`` iff that extra row was present. Exact at the boundary: exactly
    ``limit`` matching rows reports ``truncated=False``.
    """
    return rows[:limit], len(rows) > limit


def invalid_cursor_error(object_id: str) -> ToolResponse:
    """Build the ``invalid_cursor`` ``configuration_error`` for a bad list cursor (ADR-0192)."""
    return config_error_reason(object_id, ConfigErrorReason.INVALID_CURSOR)


def encode_ts_uuid_cursor(tool_tag: str, created_at: datetime, row_id: UUID) -> str:
    """Encode a ``(created_at, id)`` keyset cursor for a timestamp-ordered list (ADR-0192).

    The timestamp is serialized at full (tz-aware, microsecond) precision so it round-trips
    to the exact stored value and the ``created_at = boundary`` arm of the seek matches the
    boundary row.
    """
    return encode_cursor(tool_tag, (created_at.isoformat(), str(row_id)))


def decode_ts_uuid_cursor(tool_tag: str, cursor: str) -> tuple[datetime, UUID]:
    """Decode a ``(created_at, id)`` keyset cursor into typed seek values (ADR-0192).

    Returns the typed boundary so the handler binds a ``timestamptz`` / ``uuid`` (not a raw
    string) into the row-value seek predicate. Raises :class:`InvalidCursor` on a malformed
    or wrong-tool token, or when a well-formed token carries a non-timestamp / non-uuid part.
    """
    ts_part, id_part = decode_cursor(tool_tag, cursor, arity=2)
    try:
        return datetime.fromisoformat(ts_part), UUID(id_part)
    except ValueError as exc:
        raise InvalidCursor("cursor key parts are not a (timestamp, uuid)") from exc


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


def capability_unsupported(
    object_id: str, *, capability: str, provider: str, supported: list[str]
) -> ToolResponse:
    """A ``configuration_error`` for a plane/method the bound provider cannot serve (ADR-0209).

    Capability-aware admission (ADR-0209, building on the ADR-0208 descriptor) rejects an
    unsupported plane/method **before** enqueue/execution and maps it to the existing
    ``CONFIGURATION_ERROR`` — a caller/configuration mismatch, not a runtime dependency fault
    (ADR-0097). ``data`` carries the machine-readable ``reason: capability_unsupported`` plus the
    requested ``capability`` token (e.g. ``capture_method:host_dump``, ``debug_transport:gdbstub``,
    ``introspection:live``), the bound ``provider`` name, and the provider's ``supported`` set for
    that plane (sorted for a stable wire order). ``detail`` is a fixed template naming the provider
    and capability — the values are provider-derived enum-like tokens, never a secret, hostname,
    object-store key, or caller-un-supplied resource name (ADR-0123), so they are safe to echo.
    """
    ordered = sorted(supported)
    data: dict[str, JsonValue] = {
        "reason": "capability_unsupported",
        "capability": capability,
        "provider": provider,
        "supported": [value for value in ordered],
    }
    detail = (
        f"provider {provider!r} does not support {capability!r}; "
        f"supported on this provider: {', '.join(ordered) or '(none)'}"
    )
    return ToolResponse.failure(
        object_id, ErrorCategory.CONFIGURATION_ERROR, detail=detail, data=data
    )


def kdump_capability_refusal(object_id: str, *, capability: dict[str, JsonValue]) -> ToolResponse:
    """Refuse a kdump capture on a confidently-incapable image (``configuration_error``, ADR-0361).

    A kdump/fadump ``vmcore.fetch`` whose booted rootfs image's computed kdump capability is
    confidently negative (``incapable`` — its ``makedumpfile`` is too old — or ``not_applicable``
    — it ships no kdump tooling) is a caller/configuration mismatch, not a runtime dependency
    fault, so it maps to ``configuration_error`` (mirroring ADR-0209 ``capability_unsupported``).
    ``data.reason`` is the machine-readable ``kdump_incapable`` token; ``data.kdump_capability`` is
    the full computed block (status, versions, note) so the refusal discloses exactly why, and
    ``suggested_next_actions`` points at ``images.describe``, which renders the same signal. The
    values are computed/provenance tokens (a status, a version string, a fixed note), never a
    secret, hostname, or object-store key (ADR-0123), so they are safe to echo.
    """
    data: dict[str, JsonValue] = {
        "reason": ConfigErrorReason.KDUMP_INCAPABLE.value,
        "kdump_capability": capability,
    }
    return ToolResponse.failure(
        object_id,
        ErrorCategory.CONFIGURATION_ERROR,
        detail=(
            "the booted rootfs image cannot produce a kdump vmcore for this kernel; "
            "call images.describe to read its computed kdump capability"
        ),
        data=data,
        suggested_next_actions=["images.describe"],
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
    "InvalidCursor",
    "as_uuid",
    "authorizing",
    "authz_denied",
    "capability_unsupported",
    "clamp_list_limit",
    "config_error",
    "config_error_reason",
    "context_from_job",
    "decode_cursor",
    "decode_ts_uuid_cursor",
    "encode_cursor",
    "encode_ts_uuid_cursor",
    "invalid_cursor_error",
    "invalid_uuid_error",
    "job_envelope",
    "kdump_capability_refusal",
    "not_found",
    "paginate",
    "stale_handle",
]
