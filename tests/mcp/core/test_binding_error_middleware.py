"""Boundary binding middleware: re-envelope a binding ValidationError (ADR-0124, ADR-0132).

FastMCP validates a tool's typed params at argument binding — *before* the tool body and before any
in-body catch that builds our envelope. ``BindingErrorMiddleware`` catches that
``pydantic.ValidationError`` for the tools that need it and returns the standard
``configuration_error`` envelope (reusing ADR-0123's ``detail`` surfacing), instead of letting a raw
FastMCP ``ToolError`` reach the caller: the three typed-profile tools (``ProvisioningProfile`` is
``extra="forbid"``) and ``allocations.request`` (the shape-XOR-custom rule, ADR-0132). Any other
tool, any non-recognised binding error (a field-level error on the same payload), or any other
exception passes through unchanged.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.tools.base import ToolResult
from pydantic import BaseModel, ConfigDict, ValidationError

from kdive.domain.errors import ErrorCategory
from kdive.mcp.middleware import BindingErrorMiddleware
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tool_payloads import AllocationRequestPayload
from kdive.profiles.provisioning import ProvisioningProfile


def _envelope(result: ToolResult) -> dict[str, Any]:
    """Extract the structured-content envelope a short-circuiting middleware returns."""
    assert isinstance(result, ToolResult)
    content = result.structured_content
    assert isinstance(content, dict)
    return content


class _FakeMessage:
    def __init__(self, name: str, arguments: dict[str, object] | None = None) -> None:
        self.name = name
        self.arguments = arguments


class _FakeContext:
    def __init__(self, tool: str, arguments: dict[str, object] | None = None) -> None:
        self.message = _FakeMessage(tool, arguments)


class _CallModel(BaseModel):
    """Mirrors the binding model FastMCP builds: a ``profile`` field of the real type."""

    model_config = ConfigDict(extra="forbid")
    profile: ProvisioningProfile


def _profile_validation_error() -> ValidationError:
    """A raw pydantic ``ValidationError`` whose locations are under ``profile``.

    This is exactly the shape FastMCP raises at argument binding for a malformed typed profile —
    every error ``loc`` starts with ``"profile"``.
    """
    try:
        _CallModel.model_validate({"profile": {"schema_version": 1}})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a ValidationError")


def _non_profile_validation_error() -> ValidationError:
    """A ValidationError whose locations are NOT under ``profile`` (must propagate unchanged)."""

    class _Other(BaseModel):
        model_config = ConfigDict(extra="forbid")
        x: int

    try:
        _Other.model_validate({"bogus": 1})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a ValidationError")


def _drive(tool: str, arguments: dict[str, object] | None, exc: BaseException) -> Any:
    mw = BindingErrorMiddleware()

    async def _call_next(_ctx: Any) -> Any:
        raise exc

    async def _run() -> Any:
        return await mw.on_call_tool(_FakeContext(tool, arguments), _call_next)

    return asyncio.run(_run())


def test_binding_error_on_typed_profile_tool_becomes_configuration_error() -> None:
    envelope = _envelope(
        _drive(
            "systems.define",
            {"allocation_id": "alloc-1", "profile": {"bogus": 1}},
            _profile_validation_error(),
        )
    )
    assert envelope["status"] == "error"
    assert envelope["error_category"] == ErrorCategory.CONFIGURATION_ERROR.value
    assert envelope["object_id"] == "alloc-1"  # the call's allocation_id, not the tool name
    assert envelope["detail"]  # ADR-0123 detail is non-empty
    errors = envelope["data"].get("errors")
    assert isinstance(errors, list) and errors  # field-path entries surfaced
    assert all(set(e) <= {"loc", "msg", "type"} for e in errors)  # no input/ctx echoed


def test_reprovision_uses_system_id_as_object_id() -> None:
    envelope = _envelope(
        _drive(
            "systems.reprovision",
            {"system_id": "sys-9", "profile": {"bogus": 1}},
            _profile_validation_error(),
        )
    )
    assert envelope["object_id"] == "sys-9"


def test_missing_id_argument_falls_back_to_tool_name() -> None:
    envelope = _envelope(
        _drive("systems.provision", {"profile": {"bogus": 1}}, _profile_validation_error())
    )
    assert envelope["object_id"] == "systems.provision"


def test_validation_error_on_other_tool_is_reraised() -> None:
    exc = _profile_validation_error()
    with pytest.raises(ValidationError):
        _drive("jobs.get", {"job_id": "j1"}, exc)


def test_non_validation_error_is_reraised() -> None:
    boom = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        _drive("systems.define", {"allocation_id": "a1"}, boom)


def test_non_profile_validation_error_on_typed_tool_is_reraised() -> None:
    # A ValidationError whose locations are not under `profile` is not a binding failure (the tool
    # bodies never let a raw ValidationError escape) — it must propagate, not be mislabeled.
    with pytest.raises(ValidationError):
        _drive("systems.define", {"allocation_id": "a1"}, _non_profile_validation_error())


def test_valid_call_passes_through_unchanged() -> None:
    mw = BindingErrorMiddleware()
    sentinel = ToolResponse.success("ok", "ok")

    async def _call_next(_ctx: Any) -> Any:
        return sentinel

    async def _run() -> Any:
        return await mw.on_call_tool(_FakeContext("systems.define", {}), _call_next)

    assert asyncio.run(_run()) is sentinel


def _shape_xor_validation_error(payload: dict[str, object]) -> ValidationError:
    """The shape-XOR ``ValidationError`` FastMCP raises binding ``AllocationRequestPayload``."""
    try:
        AllocationRequestPayload.model_validate(payload)
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a ValidationError")


def test_shape_and_custom_together_becomes_configuration_error_naming_both() -> None:
    envelope = _envelope(
        _drive(
            "allocations.request",
            {"project": "demo"},
            _shape_xor_validation_error(
                {"shape": "medium", "vcpus": 2, "memory_gb": 4, "disk_gb": 20}
            ),
        )
    )
    assert envelope["status"] == "error"
    assert envelope["error_category"] == ErrorCategory.CONFIGURATION_ERROR.value
    assert envelope["object_id"] == "demo"  # the call's project
    assert "both" in envelope["detail"]
    assert "exactly one sizing source" in envelope["detail"]


def test_neither_shape_nor_custom_becomes_configuration_error_naming_neither() -> None:
    envelope = _envelope(
        _drive("allocations.request", {"project": "demo"}, _shape_xor_validation_error({}))
    )
    assert envelope["error_category"] == ErrorCategory.CONFIGURATION_ERROR.value
    assert "neither" in envelope["detail"]
    assert "exactly one sizing source" in envelope["detail"]


def test_field_level_error_on_allocations_request_is_reraised_not_collapsed() -> None:
    # A non-XOR payload error (an unknown extra field under extra='forbid') must keep FastMCP's
    # per-field detail — it must NOT be collapsed into the generic shape-XOR message (ADR-0132).
    field_error = _shape_xor_validation_error({"shape": "medium", "bogus_field": 1})
    # Sanity: this is a field-level error, not (only) the XOR error.
    assert any(err["type"] != "shape_xor_custom" for err in field_error.errors())
    with pytest.raises(ValidationError):
        _drive("allocations.request", {"project": "demo"}, field_error)


def test_end_to_end_malformed_profile_returns_envelope_not_toolerror() -> None:
    # The integration proof: a typed-profile tool behind the middleware returns the envelope for a
    # malformed profile rather than raising a client-side ToolError.
    from kdive.mcp.app import _advertise_flat_output_schema

    app: FastMCP = FastMCP(name="probe")
    app.add_middleware(BindingErrorMiddleware())

    @app.tool(name="systems.define")
    async def _define(allocation_id: str, profile: ProvisioningProfile) -> ToolResponse:
        return ToolResponse.success(allocation_id, "ok")

    # The real build_app sweeps every tool to a flat output schema (ADR-0113); apply it here so the
    # client can parse the structured envelope (the recursive ToolResponse schema would otherwise
    # break the per-call TypeAdapter).
    _advertise_flat_output_schema(app)

    async def _run() -> dict[str, Any] | None:
        async with Client(app) as client:
            result = await client.call_tool(
                "systems.define",
                {"allocation_id": "alloc-1", "profile": {"schema_version": 1}},
            )
            return result.data

    data = asyncio.run(_run())
    assert data is not None
    assert data["status"] == "error"
    assert data["error_category"] == ErrorCategory.CONFIGURATION_ERROR.value
    assert data["detail"]
