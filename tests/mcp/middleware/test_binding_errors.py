"""Cover the binding-time ValidationError conversion middleware."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastmcp.tools.base import ToolResult
from pydantic import BaseModel, Field, ValidationError

from kdive.domain.errors import ErrorCategory
from kdive.mcp.middleware.binding_errors import (
    BindingErrorMiddleware,
    _binding_object_id,
    _build_profile_envelope,
    _is_shape_xor_error,
    _loc_under,
    _profile_envelope,
    _shape_xor_envelope,
)
from kdive.mcp.tool_payloads import SHAPE_XOR_ERROR_TYPE


class _Errs(Exception):
    """A stand-in ValidationError exposing a fixed ``errors()`` payload."""

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self._entries = entries

    def errors(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return self._entries


def _ve(entries: list[dict[str, Any]]) -> ValidationError:
    """The predicates/envelopes only consume ``.errors()``; drive them with a typed stand-in."""
    return cast("ValidationError", _Errs(entries))


# --- _loc_under -------------------------------------------------------------


def test_loc_under_true_when_all_errors_under_param() -> None:
    predicate = _loc_under("profile")
    assert predicate(_ve([{"loc": ("profile", "kernel")}, {"loc": ("profile",)}]))


def test_loc_under_false_when_an_error_is_under_another_param() -> None:
    predicate = _loc_under("profile")
    assert not predicate(_ve([{"loc": ("profile",)}, {"loc": ("system_id",)}]))


def test_loc_under_false_when_no_errors() -> None:
    assert not _loc_under("profile")(_ve([]))


def test_loc_under_false_when_loc_missing() -> None:
    assert not _loc_under("profile")(_ve([{"type": "x"}]))


# --- _is_shape_xor_error ----------------------------------------------------


def test_is_shape_xor_error_true_when_all_entries_match() -> None:
    assert _is_shape_xor_error(_ve([{"type": SHAPE_XOR_ERROR_TYPE}]))


def test_is_shape_xor_error_false_for_other_type() -> None:
    assert not _is_shape_xor_error(_ve([{"type": "int_parsing"}]))


def test_is_shape_xor_error_false_when_empty() -> None:
    assert not _is_shape_xor_error(_ve([]))


# --- envelopes --------------------------------------------------------------


def test_profile_envelope_is_configuration_error_carrying_errors() -> None:
    resp = _profile_envelope("alloc-1", _ve([{"loc": ("profile",), "msg": "bad"}]))
    assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
    assert resp.object_id == "alloc-1"


def test_build_profile_envelope_is_configuration_error() -> None:
    resp = _build_profile_envelope("sys-1", _ve([{"loc": ("build_profile",)}]))
    assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
    assert resp.object_id == "sys-1"


def test_shape_xor_envelope_both_branch() -> None:
    resp = _shape_xor_envelope("proj", _ve([{"ctx": {"both": True}}]))
    assert "both a shape and a custom size" in (resp.detail or "")


def test_shape_xor_envelope_neither_branch() -> None:
    resp = _shape_xor_envelope("proj", _ve([{"ctx": {}}]))
    assert "neither a shape nor" in (resp.detail or "")


# --- _binding_object_id -----------------------------------------------------


def _ctx(name: str, arguments: Any) -> Any:
    return SimpleNamespace(message=SimpleNamespace(name=name, arguments=arguments))


def test_binding_object_id_reads_string_argument() -> None:
    ctx = _ctx("systems.define", {"allocation_id": "a-1"})
    assert _binding_object_id(ctx, (("allocation_id",),)) == "a-1"


def test_binding_object_id_reads_nested_string_argument() -> None:
    ctx = _ctx("runs.create", {"request": {"system_id": "s-1"}})
    assert _binding_object_id(ctx, (("request", "system_id"), ("system_id",))) == "s-1"


def test_binding_object_id_falls_back_to_tool_name_for_non_string() -> None:
    ctx = _ctx("systems.define", {"allocation_id": 5})
    assert _binding_object_id(ctx, (("allocation_id",),)) == "systems.define"


def test_binding_object_id_falls_back_when_arguments_not_a_dict() -> None:
    assert (
        _binding_object_id(_ctx("systems.define", None), (("allocation_id",),)) == "systems.define"
    )


# --- on_call_tool (real ValidationError) ------------------------------------


class _ProfileModel(BaseModel):
    profile: int = Field(ge=0)


class _OtherModel(BaseModel):
    other: int = Field(ge=0)


def _profile_range_error() -> ValidationError:
    try:
        _ProfileModel(profile=-1)
    except ValidationError as exc:
        return exc
    raise AssertionError("expected ValidationError")


def _non_profile_error() -> ValidationError:
    try:
        _OtherModel(other=-1)
    except ValidationError as exc:
        return exc
    raise AssertionError("expected ValidationError")


def test_on_call_tool_passes_through_unregistered_tool() -> None:
    mw = BindingErrorMiddleware()
    sentinel = object()

    async def call_next(_ctx: Any) -> Any:
        return sentinel

    assert asyncio.run(mw.on_call_tool(_ctx("runs.get", {}), call_next)) is sentinel


def test_on_call_tool_envelopes_matching_binding_error() -> None:
    mw = BindingErrorMiddleware()

    async def call_next(_ctx: Any) -> Any:
        raise _profile_range_error()

    result = asyncio.run(
        mw.on_call_tool(_ctx("systems.define", {"allocation_id": "a-1"}), call_next)
    )
    assert isinstance(result, ToolResult)
    assert result.structured_content["error_category"] == ErrorCategory.CONFIGURATION_ERROR.value
    assert result.structured_content["object_id"] == "a-1"


def test_on_call_tool_reraises_non_matching_validation_error() -> None:
    mw = BindingErrorMiddleware()

    async def call_next(_ctx: Any) -> Any:
        raise _non_profile_error()

    with pytest.raises(ValidationError):
        asyncio.run(mw.on_call_tool(_ctx("systems.define", {"allocation_id": "a-1"}), call_next))
