"""A server fault in a tool body is reported as one, not as an argument error (ADR-0709).

Driven through a real in-process FastMCP app, because the misreport this guards against is
FastMCP's own: it logs an escaping pydantic ``ValidationError`` as ``Invalid arguments for tool``
before any middleware runs.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from pydantic import BaseModel

from kdive.domain.errors import ErrorCategory
from kdive.mcp.middleware.binding_errors import BindingErrorMiddleware
from kdive.mcp.middleware.server_fault import SERVER_FAULT_DETAIL, ServerFaultMiddleware
from kdive.mcp.responses import ToolResponse, validate_stored

_ARGUMENT_ERROR_LOG = "Invalid arguments for tool"


class _Row(BaseModel):
    profile: int


@pytest.fixture
def fastmcp_log(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    """Capture FastMCP's records; its logger does not propagate to the root caplog handler."""
    logger = logging.getLogger("fastmcp")
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG)
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)


def _app() -> FastMCP:
    # Async bodies, as every kdive tool is: FastMCP runs them on a different path than sync ones.
    app: FastMCP = FastMCP("t")
    app.add_middleware(BindingErrorMiddleware())
    app.add_middleware(ServerFaultMiddleware())

    @app.tool(name="bad")
    async def bad() -> ToolResponse:
        return ToolResponse(object_id="x", status="queued", error_category="not_found")

    @app.tool(name="replay")
    async def replay() -> ToolResponse:
        return ToolResponse.model_validate({"object_id": "x", "status": "failed"})

    @app.tool(name="stored")
    async def stored() -> ToolResponse:
        row = validate_stored(_Row, {"profile": "not-an-int"})
        return ToolResponse.success(str(row.profile), "ok")

    # A caller-input rebuild in a tool BindingErrorMiddleware converts (#2981 keeps its path).
    @app.tool(name="systems.provision")
    async def provision(allocation_id: str) -> ToolResponse:
        _Row.model_validate({"profile": "not-an-int"})
        return ToolResponse.success(allocation_id, "ok")

    @app.tool(name="typed")
    async def typed(n: int) -> ToolResponse:
        return ToolResponse.success(str(n), "ok")

    return app


def _call(name: str, arguments: dict[str, Any]) -> Any:
    async def _run() -> Any:
        async with Client(_app()) as client:
            return await client.call_tool(name, arguments, raise_on_error=False)

    return asyncio.run(_run())


@pytest.mark.parametrize("tool", ["bad", "replay", "stored"])
def test_server_fault_returns_a_server_fault_envelope(
    tool: str, fastmcp_log: pytest.LogCaptureFixture
) -> None:
    result = _call(tool, {})

    assert not result.is_error
    envelope = result.structured_content
    assert envelope["object_id"] == tool
    assert envelope["status"] == "error"
    assert envelope["error_category"] == ErrorCategory.INFRASTRUCTURE_FAILURE.value
    assert envelope["detail"] == SERVER_FAULT_DETAIL
    errors = [r for r in fastmcp_log.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    assert not [r for r in fastmcp_log.records if _ARGUMENT_ERROR_LOG in r.getMessage()]


def test_caller_input_rebuild_keeps_binding_envelope(
    fastmcp_log: pytest.LogCaptureFixture,
) -> None:
    result = _call("systems.provision", {"allocation_id": "a-1"})

    assert not result.is_error
    envelope = result.structured_content
    assert envelope["object_id"] == "a-1"
    assert envelope["error_category"] == ErrorCategory.CONFIGURATION_ERROR.value
    assert not [r for r in fastmcp_log.records if r.levelno == logging.ERROR]


def test_argument_error_is_still_reported_as_one(fastmcp_log: pytest.LogCaptureFixture) -> None:
    result = _call("typed", {"n": "abc"})

    assert result.is_error
    warnings = [r for r in fastmcp_log.records if _ARGUMENT_ERROR_LOG in r.getMessage()]
    assert [r.levelno for r in warnings] == [logging.WARNING]
