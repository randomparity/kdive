"""Caller-validation provenance is explicit, not inferred from field names (ADR-0844)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.exceptions import ValidationError as FastMCPValidationError
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ValidationError

from kdive.mcp.middleware.binding_errors import BindingErrorMiddleware, binding_validation_error
from kdive.mcp.middleware.server_fault import ServerFaultMiddleware
from kdive.mcp.responses import ToolResponse
from kdive.mcp.schema.tool_payloads import AllocationRequestPayload
from kdive.mcp.tools import gateway
from kdive.mcp.tools.lifecycle.allocations import registrar as allocations_registrar
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.authz.rbac import Role
from tests.mcp.middleware.binding_support import wrap_validation_error
from tests.mcp.systems_support import ctx, provider_resolver


class StoredRow(BaseModel):
    profile: int


@pytest.mark.parametrize("via_gateway", [False, True], ids=["direct", "gateway"])
@pytest.mark.parametrize(
    "bad_binding", [False, True], ids=["body-error", "genuine-binding-control"]
)
def test_curated_location_does_not_establish_caller_provenance(
    via_gateway: bool, bad_binding: bool
) -> None:
    app = FastMCP("binding-provenance-proof")
    app.add_middleware(BindingErrorMiddleware())
    app.add_middleware(ServerFaultMiddleware())

    @app.tool(name="systems.provision")
    async def provision(allocation_id: str, profile: int) -> ToolResponse:
        row = StoredRow.model_validate({"profile": "stored-bad-value"})
        return ToolResponse.success(allocation_id, "ok", data={"profile": row.profile + profile})

    gateway.register(app, resolver=ProviderResolver({}))

    async def run() -> None:
        arguments: dict[str, Any] = {
            "allocation_id": "allocation-proof",
            "profile": "caller-bad-value" if bad_binding else 1,
        }
        name = "tools.invoke" if via_gateway else "systems.provision"
        payload = (
            {"name": "systems.provision", "arguments": arguments} if via_gateway else arguments
        )
        if not via_gateway and not bad_binding:
            with pytest.raises(ValidationError):
                await app.call_tool(name, payload, run_middleware=True)
            return
        result = await app.call_tool(name, payload, run_middleware=True)
        envelope = result.structured_content
        assert isinstance(envelope, dict)
        if bad_binding:
            assert envelope["object_id"] == "allocation-proof"
            assert envelope["detail"] == "invalid provisioning profile"
            assert envelope["data"]["errors"]
        else:
            assert envelope["object_id"] == "tools.invoke", "body error became a caller binding"
            assert envelope["data"] == {}
            assert envelope["suggested_next_actions"] == []

    asyncio.run(run())


@pytest.mark.parametrize("shape", ["bare", "missing", "wrong", "deep", "other-outer", "context"])
def test_nonbinding_cause_shapes_propagate_with_identity(shape: str) -> None:
    with pytest.raises(ValidationError) as caught:
        StoredRow.model_validate({"profile": "stored-bad-value"})
    raw = caught.value
    if shape == "bare":
        error: Exception = raw
    elif shape == "missing":
        error = wrap_validation_error(None)
    elif shape == "wrong":
        error = wrap_validation_error(ValueError("body failure"))
    elif shape == "deep":
        error = wrap_validation_error(wrap_validation_error(raw))
    elif shape == "other-outer":
        error = ToolError("body failure")
        error.__cause__ = raw
    else:
        error = wrap_validation_error(None)
        error.__context__ = raw
    assert binding_validation_error(error) is None

    async def call_next(_context: Any) -> Any:
        raise error

    context = SimpleNamespace(
        message=SimpleNamespace(
            name="systems.provision", arguments={"allocation_id": "allocation-proof"}
        )
    )
    with pytest.raises(type(error)) as propagated:
        asyncio.run(BindingErrorMiddleware().on_call_tool(context, call_next))
    assert propagated.value is error


def test_wrapped_validation_cause_is_returned_unchanged() -> None:
    with pytest.raises(ValidationError) as caught:
        StoredRow.model_validate({"profile": "caller-bad-value"})
    assert binding_validation_error(wrap_validation_error(caught.value)) is caught.value


@pytest.mark.parametrize("via_gateway", [False, True], ids=["direct", "gateway"])
@pytest.mark.parametrize("sizing", ["both", "neither", "valid"])
def test_real_allocation_wrapper_marks_only_its_caller_reconstruction(
    monkeypatch: pytest.MonkeyPatch, via_gateway: bool, sizing: str
) -> None:
    seen: list[AllocationRequestPayload] = []

    async def handler(
        _pool: Any,
        _context: Any,
        *,
        project: str,
        request: AllocationRequestPayload,
        **_kwargs: Any,
    ) -> ToolResponse:
        seen.append(request)
        return ToolResponse.success(project, "constructed")

    monkeypatch.setattr(allocations_registrar, "current_context", lambda: ctx(Role.OPERATOR))
    # This test owns wrapper construction/dispatch; handler admission has its real-DB suite.
    monkeypatch.setattr(allocations_registrar, "_request_allocation", handler)
    resolver = provider_resolver()
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = FastMCP("allocation-caller-provenance")
    app.add_middleware(BindingErrorMiddleware())
    app.add_middleware(ServerFaultMiddleware())
    allocations_registrar.register(app, pool, resolver=resolver)
    gateway.register(app, resolver=resolver)
    arguments: dict[str, Any] = {"project": "proj"}
    if sizing != "neither":
        arguments["shape"] = "small"
    if sizing == "both":
        arguments.update(vcpus=2, memory_gb=4, disk_gb=20)

    async def run() -> None:
        try:
            name = "tools.invoke" if via_gateway else "allocations.request"
            payload = (
                {"name": "allocations.request", "arguments": arguments}
                if via_gateway
                else arguments
            )
            result = await app.call_tool(name, payload, run_middleware=True)
            envelope = result.structured_content
            assert isinstance(envelope, dict)
            assert envelope["object_id"] == "proj"
            if sizing == "valid":
                assert envelope["status"] == "constructed"
                assert len(seen) == 1 and seen[0].shape == "small"
            else:
                assert envelope["error_category"] == "configuration_error"
                assert sizing in envelope["detail"]
                assert "exactly one sizing source" in envelope["detail"]
                assert not seen
        finally:
            await pool.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "tool", ["jobs.get", "systems.provision", "runs.create", "allocations.request"]
)
def test_unmatched_wrapped_errors_propagate_with_identity(tool: str) -> None:
    with pytest.raises(ValidationError) as caught:
        AllocationRequestPayload.model_validate({"shape": "small", "unexpected": 1})
    error = wrap_validation_error(caught.value)
    assert binding_validation_error(error) is caught.value

    async def call_next(_context: Any) -> Any:
        raise error

    context = SimpleNamespace(message=SimpleNamespace(name=tool, arguments={}))
    with pytest.raises(FastMCPValidationError) as propagated:
        asyncio.run(BindingErrorMiddleware().on_call_tool(context, call_next))
    assert propagated.value is error
