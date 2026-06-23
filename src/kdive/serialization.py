"""Shared JSON value contracts for database and MCP serialization boundaries."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import cast

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]

# Pydantic-validation-error sub-keys preserved when an `errors` list is surfaced (ADR-0123).
# `input` and `ctx` are deliberately excluded so no submitted value or internal context echoes.
_ERROR_ENTRY_KEYS = ("loc", "msg", "type")
# Upper bound on surfaced validation-error entries (ADR-0123): a bounded reason, never a dump.
_MAX_ERROR_ENTRIES = 20
# Detail keys whose list value is preserved as a bounded list of JSON scalars (ADR-0224, #731).
# These carry a finite valid set (declared catalog names, configured roots) so a black-box MCP
# caller can self-correct a typo'd reference; non-scalar elements are dropped and the list is
# capped at `_MAX_ERROR_ENTRIES`. Every other list-valued detail key is still dropped.
_ENUMERATION_KEYS = frozenset({"accepted_values", "available"})


def validate_json_value(value: object, *, path: str) -> None:
    """Validate that ``value`` is a concrete JSON tree."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must be finite JSON number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            validate_json_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} object keys must be strings")
            validate_json_value(item, path=f"{path}.{key}")
        return
    raise ValueError(f"{path} contains non-JSON value {type(value).__name__}")


def ensure_json_value(value: object, *, path: str) -> JsonValue:
    """Return ``value`` typed as JSON after validating its runtime shape."""
    validate_json_value(value, path=path)
    return cast(JsonValue, value)


def _scalar_or_none(value: object) -> JsonValue | None:
    """Return ``value`` if it is a finite JSON scalar, else ``None``.

    ``bool`` is an ``int`` subclass; the ``float`` branch runs first so a non-finite float is
    dropped before the ``(str, bool, int)`` branch preserves booleans as booleans.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (str, bool, int)):
        return value
    return None


def _sanitize_loc(loc: object) -> JsonValue:
    """Render a Pydantic error ``loc`` to a JSON list of path segments.

    Integer segments (list indices) are kept as ints; every other segment is stringified, so the
    surfaced field path stays JSON-trivial. A non-sequence ``loc`` is stringified whole.
    """
    if isinstance(loc, (tuple, list)):
        return [segment if isinstance(segment, int) else str(segment) for segment in loc]
    return str(loc)


def _sanitize_error_entry(entry: object) -> dict[str, JsonValue] | None:
    """Reduce one validation-error entry to ``{loc, msg, type}`` with scalar values.

    Only the reserved sub-keys are kept; ``input``/``ctx``/``url`` and any other key are dropped
    so no submitted value or internal context reaches the wire. Non-mapping entries are skipped.
    """
    if not isinstance(entry, Mapping):
        return None
    mapping = cast(Mapping[object, object], entry)
    safe: dict[str, JsonValue] = {}
    for key in _ERROR_ENTRY_KEYS:
        if key not in mapping:
            continue
        if key == "loc":
            safe["loc"] = _sanitize_loc(mapping[key])
            continue
        scalar = _scalar_or_none(mapping[key])
        if scalar is not None:
            safe[key] = scalar
    return safe


def safe_error_details(details: Mapping[str, object]) -> dict[str, JsonValue]:
    """Filter ``CategorizedError`` details to a JSON-safe payload (ADR-0019, ADR-0123).

    Every key is reduced to a finite JSON scalar and non-scalars are dropped, with two reserved
    exceptions: an ``errors`` list (the shape ``ProvisioningProfile.parse`` emits) is preserved as
    a bounded list of ``{loc, msg, type}`` entries so a caller learns the exact bad field paths;
    an ``accepted_values`` / ``available`` list (ADR-0224) is preserved as a bounded list of JSON
    scalars so a caller learns the finite valid set (declared catalog names, configured roots).
    Both lists drop non-scalar elements and cap at ``_MAX_ERROR_ENTRIES``. No submitted value
    echoes back — ``input``/``ctx`` are never forwarded.
    """
    safe: dict[str, JsonValue] = {}
    for key, value in details.items():
        if key == "errors" and isinstance(value, list):
            entries = [_sanitize_error_entry(entry) for entry in value[:_MAX_ERROR_ENTRIES]]
            safe["errors"] = [entry for entry in entries if entry is not None]
            continue
        if key in _ENUMERATION_KEYS and isinstance(value, list):
            scalars = (_scalar_or_none(item) for item in value[:_MAX_ERROR_ENTRIES])
            safe[key] = [item for item in scalars if item is not None]
            continue
        scalar = _scalar_or_none(value)
        if scalar is not None:
            safe[key] = scalar
    return safe
