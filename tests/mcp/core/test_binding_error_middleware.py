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
from kdive.mcp.middleware.binding_errors import BindingErrorMiddleware
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tool_payloads import AllocationRequestPayload
from kdive.profiles.build import BuildProfile
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


class _BuildCallModel(BaseModel):
    """Mirrors the binding model FastMCP builds for ``runs.create``: a typed ``build_profile``."""

    model_config = ConfigDict(extra="forbid")
    build_profile: BuildProfile


def _build_profile_validation_error() -> ValidationError:
    """A raw pydantic ``ValidationError`` whose locations are under ``build_profile``.

    Exactly the shape FastMCP raises at argument binding for a malformed ``runs.create``
    ``build_profile`` — every error ``loc`` starts with ``"build_profile"`` (then the field).
    """
    try:
        _BuildCallModel.model_validate({"build_profile": {"schema_version": 1, "bogus": 1}})
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


def test_runs_create_build_profile_binding_becomes_configuration_error() -> None:
    envelope = _envelope(
        _drive(
            "runs.create",
            {"system_id": "sys-1", "build_profile": {"schema_version": 1}},
            _build_profile_validation_error(),
        )
    )
    assert envelope["status"] == "error"
    assert envelope["error_category"] == ErrorCategory.CONFIGURATION_ERROR.value
    assert envelope["object_id"] == "sys-1"  # the call's system_id, not the tool name
    # detail matches the in-body BuildProfile.parse message, not the provisioning one
    assert envelope["detail"] == "invalid build profile"
    errors = envelope["data"].get("errors")
    assert isinstance(errors, list) and errors  # field-path entries surfaced
    assert all(set(e) <= {"loc", "msg", "type"} for e in errors)  # no input/ctx echoed


def test_runs_create_non_build_profile_validation_error_is_reraised() -> None:
    # A ValidationError whose locations are not under `build_profile` is not a binding failure;
    # the create body never lets a raw ValidationError escape, so it must propagate, not mislabel.
    with pytest.raises(ValidationError):
        _drive("runs.create", {"system_id": "sys-1"}, _non_profile_validation_error())


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
    from kdive.mcp.schema_advertising import advertise_envelope_output_schema

    app: FastMCP = FastMCP(name="probe")
    app.add_middleware(BindingErrorMiddleware())

    @app.tool(name="systems.define")
    async def _define(allocation_id: str, profile: ProvisioningProfile) -> ToolResponse:
        return ToolResponse.success(allocation_id, "ok")

    # The real build_app sweeps every tool to the fielded envelope output schema (ADR-0170); apply
    # it here so the client can parse the structured envelope (the recursive ToolResponse schema
    # would otherwise break the per-call TypeAdapter). The fielded schema makes `result.data` a
    # pydantic model, so read the byte-stable envelope off `structured_content`.
    advertise_envelope_output_schema(app)

    async def _run() -> dict[str, Any] | None:
        async with Client(app) as client:
            result = await client.call_tool(
                "systems.define",
                {"allocation_id": "alloc-1", "profile": {"schema_version": 1}},
            )
            return result.structured_content

    data = asyncio.run(_run())
    assert data is not None
    assert data["status"] == "error"
    assert data["error_category"] == ErrorCategory.CONFIGURATION_ERROR.value
    assert data["detail"]


def test_end_to_end_runs_create_typed_build_profile_publishes_schema_and_envelopes() -> None:
    # The integration proof for #482: runs.create with a typed flat `build_profile` publishes its
    # input schema, accepts a valid profile, and returns the envelope (not a ToolError) for a
    # malformed one — exercising the real _BINDING_CONVERSIONS["runs.create"] entry.
    from kdive.mcp.schema_advertising import advertise_envelope_output_schema

    app: FastMCP = FastMCP(name="probe")
    app.add_middleware(BindingErrorMiddleware())

    @app.tool(name="runs.create")
    async def _create(system_id: str, build_profile: BuildProfile) -> ToolResponse:
        return ToolResponse.success(
            system_id, "created", data={"schema_version": build_profile.schema_version}
        )

    advertise_envelope_output_schema(app)

    async def _run() -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
        async with Client(app) as client:
            tools = {t.name: t for t in await client.list_tools()}
            schema = tools["runs.create"].inputSchema["properties"]["build_profile"]
            valid = await client.call_tool(
                "runs.create",
                {"system_id": "sys-1", "build_profile": {"schema_version": 1}},
            )
            malformed = await client.call_tool(
                "runs.create",
                {"system_id": "sys-1", "build_profile": {"schema_version": 1, "bogus": 1}},
            )
            return schema, valid.structured_content, malformed.structured_content

    schema, valid_data, malformed_data = asyncio.run(_run())
    assert schema  # the build_profile schema is published, discoverable from the tool surface
    assert valid_data is not None and valid_data["status"] == "created"
    assert valid_data["data"]["schema_version"] == 1
    assert malformed_data is not None
    assert malformed_data["status"] == "error"
    assert malformed_data["error_category"] == ErrorCategory.CONFIGURATION_ERROR.value
    assert malformed_data["detail"]

    # R4 (no input echo) is asserted exactly by the fixed-template test below; a substring
    # check here is unreliable when the input digit also appears in a bound.
