"""Binding-time validation error conversion for MCP tools."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fastmcp.exceptions import ValidationError as FastMCPValidationError
from fastmcp.server.middleware import Middleware
from fastmcp.tools.base import ToolResult
from pydantic import ValidationError

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.responses import ToolResponse
from kdive.mcp.schema.tool_payloads import ShapeXorCustomError


def _loc_under(param: str) -> Callable[[ValidationError], bool]:
    """Predicate for FastMCP binding failures under one typed parameter."""

    def _predicate(exc: ValidationError) -> bool:
        errors = exc.errors()
        return bool(errors) and all(
            bool(err.get("loc")) and err["loc"][0] == param for err in errors
        )

    return _predicate


def _loc_under_path(path: Sequence[str]) -> Callable[[ValidationError], bool]:
    """Predicate for binding failures under a nested typed parameter path."""

    def _predicate(exc: ValidationError) -> bool:
        errors = exc.errors()
        return bool(errors) and all(
            bool(err.get("loc")) and tuple(err["loc"][: len(path)]) == tuple(path) for err in errors
        )

    return _predicate


def _any_match(*predicates: Callable[[ValidationError], bool]) -> Callable[[ValidationError], bool]:
    """Predicate that accepts any one binding-error shape."""

    def _predicate(exc: ValidationError) -> bool:
        return any(predicate(exc) for predicate in predicates)

    return _predicate


def _is_shape_xor_error(exc: ValidationError) -> bool:
    """Whether every error entry is the shape-XOR-custom validator error."""
    errors = exc.errors()
    return bool(errors) and all(_shape_xor_error(err) is not None for err in errors)


def _shape_xor_error(err: Mapping[str, Any]) -> ShapeXorCustomError | None:
    ctx = err.get("ctx")
    if not isinstance(ctx, dict):
        return None
    error = ctx.get("error")
    return error if isinstance(error, ShapeXorCustomError) else None


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
    both = any(error.both for err in exc.errors() if (error := _shape_xor_error(err)) is not None)
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


@dataclass(frozen=True, slots=True)
class _BindingConversion:
    """How to convert one tool's binding ``ValidationError`` into an envelope."""

    id_paths: tuple[tuple[str, ...], ...]
    matches: Callable[[ValidationError], bool]
    build: Callable[[str, ValidationError], ToolResponse]


_BINDING_CONVERSIONS: dict[str, _BindingConversion] = {
    "systems.define": _BindingConversion(
        (("allocation_id",),), _loc_under("profile"), _profile_envelope
    ),
    "systems.provision": _BindingConversion(
        (("allocation_id",),), _loc_under("profile"), _profile_envelope
    ),
    "systems.reprovision": _BindingConversion(
        (("system_id",),), _loc_under("profile"), _profile_envelope
    ),
    "runs.create": _BindingConversion(
        (("request", "system_id"), ("system_id",)),
        _any_match(_loc_under_path(("request", "build_profile")), _loc_under("build_profile")),
        _build_profile_envelope,
    ),
    "allocations.request": _BindingConversion(
        (("project",),), _is_shape_xor_error, _shape_xor_envelope
    ),
}


class BindingErrorMiddleware(Middleware):
    """Convert selected binding-time ``ValidationError`` instances into uniform envelopes."""

    async def on_call_tool(
        self,
        context: Any,
        call_next: Callable[[Any], Any],
    ) -> Any:
        """Dispatch one call; re-envelope a recognised binding ``ValidationError``.

        FastMCP raises the binding failure two ways: a tool body that rebuilds its
        payload lets a raw ``pydantic.ValidationError`` escape, while argument binding
        of a typed parameter wraps it in ``fastmcp.exceptions.ValidationError`` (with the
        pydantic error as ``__cause__``). Handle both so the envelope is produced
        regardless of which path FastMCP took.
        """
        conversion = _BINDING_CONVERSIONS.get(context.message.name)
        if conversion is None:
            return await call_next(context)
        try:
            return await call_next(context)
        except FastMCPValidationError as exc:
            cause = exc.__cause__
            if not isinstance(cause, ValidationError):
                raise
            envelope = self._enveloped(context, conversion, cause)
            if envelope is None:
                raise
            return envelope
        except ValidationError as exc:
            envelope = self._enveloped(context, conversion, exc)
            if envelope is None:
                raise
            return envelope

    @staticmethod
    def _enveloped(
        context: Any, conversion: _BindingConversion, exc: ValidationError
    ) -> ToolResult | None:
        """The uniform envelope for a recognised binding error, or None to re-raise."""
        if not conversion.matches(exc):
            return None
        object_id = _binding_object_id(context, conversion.id_paths)
        envelope = conversion.build(object_id, exc)
        return ToolResult(structured_content=envelope.model_dump(mode="json"))


def _binding_object_id(context: Any, id_paths: tuple[tuple[str, ...], ...]) -> str:
    """The call's object id from the first present path, falling back to the tool name."""
    arguments = getattr(context.message, "arguments", None)
    for path in id_paths:
        value = arguments
        for key in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        if isinstance(value, str):
            return value
    return str(context.message.name)
