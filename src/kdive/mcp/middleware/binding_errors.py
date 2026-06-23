"""Binding-time validation error conversion for MCP tools."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastmcp.server.middleware import Middleware
from fastmcp.tools.base import ToolResult
from pydantic import ValidationError

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tool_payloads import SHAPE_XOR_ERROR_TYPE
from kdive.mcp.tools._common import config_error
from kdive.security.artifacts.artifact_search import (
    AFTER_LINES_RANGE,
    BEFORE_LINES_RANGE,
    MAX_MATCHES_RANGE,
)

_SEARCH_CONTEXT_BOUNDS: dict[str, tuple[int, int]] = {
    "before_lines": BEFORE_LINES_RANGE,
    "after_lines": AFTER_LINES_RANGE,
    "max_matches": MAX_MATCHES_RANGE,
}
_RANGE_ERROR_TYPES = frozenset({"greater_than_equal", "less_than_equal"})


def _loc_under(param: str) -> Callable[[ValidationError], bool]:
    """Predicate for FastMCP binding failures under one typed parameter."""

    def _predicate(exc: ValidationError) -> bool:
        errors = exc.errors()
        return bool(errors) and all(
            bool(err.get("loc")) and err["loc"][0] == param for err in errors
        )

    return _predicate


def _is_shape_xor_error(exc: ValidationError) -> bool:
    """Whether every error entry is the shape-XOR-custom validator error."""
    errors = exc.errors()
    return bool(errors) and all(err.get("type") == SHAPE_XOR_ERROR_TYPE for err in errors)


def _profile_envelope(object_id: str, exc: ValidationError) -> ToolResponse:
    """Envelope a malformed typed-profile binding error."""
    error = CategorizedError(
        "invalid provisioning profile",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={
            "errors": exc.errors(include_url=False, include_input=False, include_context=False),
        },
    )
    return ToolResponse.failure_from_error(object_id, error)


def _build_profile_envelope(object_id: str, exc: ValidationError) -> ToolResponse:
    """Envelope a malformed ``build_profile`` binding error."""
    error = CategorizedError(
        "invalid build profile",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={
            "errors": exc.errors(include_url=False, include_input=False, include_context=False),
        },
    )
    return ToolResponse.failure_from_error(object_id, error)


def _shape_xor_envelope(object_id: str, exc: ValidationError) -> ToolResponse:
    """Envelope a shape-XOR-custom binding error with a precise ``detail``."""
    both = any(err.get("ctx", {}).get("both") for err in exc.errors())
    detail = (
        "supplied both a shape and a custom size; supply exactly one sizing source "
        "(a shape, or the full {vcpus, memory_gb, disk_gb} triple)"
        if both
        else (
            "supplied neither a shape nor a full {vcpus, memory_gb, disk_gb} triple; "
            "supply exactly one sizing source"
        )
    )
    return ToolResponse.failure(object_id, ErrorCategory.CONFIGURATION_ERROR, detail=detail)


def _is_search_context_cap_error(exc: ValidationError) -> bool:
    """Whether every error entry is an over-cap numeric-range error on a context field (ADR-0225).

    Range-only by design: an ``int_parsing`` / ``int_from_float`` error under the same ``loc`` is a
    type mismatch, not a cap rejection (the #733 non-goal), so it must not be re-enveloped as
    ``bad_search_input``.
    """
    errors = exc.errors()
    return bool(errors) and all(
        bool(err.get("loc"))
        and err["loc"][0] in _SEARCH_CONTEXT_BOUNDS
        and err.get("type") in _RANGE_ERROR_TYPES
        for err in errors
    )


def _search_context_cap_envelope(object_id: str, exc: ValidationError) -> ToolResponse:
    """Envelope an over-cap context-field binding error, naming the field and its bound (ADR-0225).

    Keeps the established ``data.reason = "bad_search_input"`` token and builds the ``detail`` from
    the offending field name plus its two integer bounds only — no caller-supplied value is echoed
    (ADR-0123/ADR-0225 R4).
    """
    field = str(exc.errors()[0]["loc"][0])
    low, high = _SEARCH_CONTEXT_BOUNDS[field]
    return config_error(
        object_id,
        detail=f"{field} must be between {low} and {high}",
        data={"reason": "bad_search_input"},
    )


@dataclass(frozen=True, slots=True)
class _BindingConversion:
    """How to convert one tool's binding ``ValidationError`` into an envelope."""

    id_arg: str
    matches: Callable[[ValidationError], bool]
    build: Callable[[str, ValidationError], ToolResponse]


_BINDING_CONVERSIONS: dict[str, _BindingConversion] = {
    "systems.define": _BindingConversion("allocation_id", _loc_under("profile"), _profile_envelope),
    "systems.provision": _BindingConversion(
        "allocation_id", _loc_under("profile"), _profile_envelope
    ),
    "systems.reprovision": _BindingConversion(
        "system_id", _loc_under("profile"), _profile_envelope
    ),
    "runs.create": _BindingConversion(
        "system_id", _loc_under("build_profile"), _build_profile_envelope
    ),
    "allocations.request": _BindingConversion("project", _is_shape_xor_error, _shape_xor_envelope),
    "artifacts.search_text": _BindingConversion(
        "artifact_id", _is_search_context_cap_error, _search_context_cap_envelope
    ),
}


class BindingErrorMiddleware(Middleware):
    """Convert selected binding-time ``ValidationError`` instances into uniform envelopes."""

    async def on_call_tool(
        self,
        context: Any,
        call_next: Callable[[Any], Any],
    ) -> Any:
        """Dispatch one call; re-envelope a recognised binding ``ValidationError``."""
        conversion = _BINDING_CONVERSIONS.get(context.message.name)
        if conversion is None:
            return await call_next(context)
        try:
            return await call_next(context)
        except ValidationError as exc:
            if not conversion.matches(exc):
                raise
            object_id = _binding_object_id(context, conversion.id_arg)
            envelope = conversion.build(object_id, exc)
            return ToolResult(structured_content=envelope.model_dump(mode="json"))


def _binding_object_id(context: Any, id_arg: str) -> str:
    """The call's object id from ``id_arg``, falling back to the tool name."""
    arguments = getattr(context.message, "arguments", None)
    if isinstance(arguments, dict):
        value = arguments.get(id_arg)
        if isinstance(value, str):
            return value
    return str(context.message.name)
