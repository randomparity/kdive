"""CompactResponseMiddleware: opt-in null/empty envelope trimming (ADR-0314, #1035)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.tools.base import ToolResult

from kdive.domain.errors import ErrorCategory
from kdive.mcp.middleware.compact import CompactResponseMiddleware
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.gateway import register as register_gateway
from kdive.providers.core.resolver import ProviderResolver


class _FakeContext:
    def __init__(self, name: str = "demo.list") -> None:
        self.message = type("_M", (), {"name": name})()


def _drive(result: Any, *, enabled: bool, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Run the middleware over a call_next that yields `result`, with the flag on/off."""
    if enabled:
        monkeypatch.setenv("KDIVE_COMPACT_RESPONSES", "on")
    else:
        monkeypatch.delenv("KDIVE_COMPACT_RESPONSES", raising=False)
    mw = CompactResponseMiddleware()

    async def _call_next(_ctx: Any) -> Any:
        return result

    return asyncio.run(mw.on_call_tool(_FakeContext(), _call_next))


def _full_collection() -> ToolResult:
    rows = [ToolResponse.success("img-0", "registered", data={"name": "n0"})]
    env = ToolResponse.collection("images", "ok", rows, suggested_next_actions=["images.list"])
    return ToolResult(structured_content=env.model_dump(mode="json"))


def test_disabled_passes_result_through_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _full_collection()
    out = _drive(result, enabled=False, monkeypatch=monkeypatch)
    assert out is result  # identical object, no rebuild


def test_enabled_compacts_collection_and_items(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _drive(_full_collection(), enabled=True, monkeypatch=monkeypatch)
    sc = out.structured_content
    # top-level defaulted empties/nulls gone; object_id/status/non-empty data kept
    assert set(sc) == {"object_id", "status", "suggested_next_actions", "data", "items"}
    assert "error_category" not in sc and "refs" not in sc and "detail" not in sc
    row = sc["items"][0]
    assert set(row) == {"object_id", "status", "data"}  # per-item empties/nulls gone


def test_enabled_compacts_content_text_block(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _drive(_full_collection(), enabled=True, monkeypatch=monkeypatch)
    assert out.content, "content block must be regenerated"
    parsed = json.loads(out.content[0].text)
    assert "error_category" not in parsed
    assert "error_category" not in parsed["items"][0]


def test_enabled_preserves_direct_failure_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    env = ToolResponse.failure("obj", ErrorCategory.NOT_FOUND)  # suppressed constant detail
    out = _drive(
        ToolResult(structured_content=env.model_dump(mode="json")),
        enabled=True,
        monkeypatch=monkeypatch,
    )
    sc = out.structured_content
    assert sc["error_category"] == ErrorCategory.NOT_FOUND.value
    assert sc["retryable"] is False
    assert sc["detail"]  # non-null suppressed constant kept


def test_enabled_drops_null_detail_on_from_job_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # A worker-plane FAILED envelope: failure status, category set, detail=None by design.
    env = ToolResponse(
        object_id="job-1",
        status="failed",
        error_category=ErrorCategory.BUILD_FAILURE.value,
    )
    out = _drive(
        ToolResult(structured_content=env.model_dump(mode="json")),
        enabled=True,
        monkeypatch=monkeypatch,
    )
    sc = out.structured_content
    assert sc["error_category"] == ErrorCategory.BUILD_FAILURE.value
    assert sc["retryable"] is False
    assert "detail" not in sc  # null detail correctly omitted


def test_enabled_passes_superset_dict_through_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    # A dict with a key the envelope does not define must NOT be stripped.
    result = ToolResult(structured_content={"object_id": "x", "status": "ok", "extra": 1})
    out = _drive(result, enabled=True, monkeypatch=monkeypatch)
    assert out is result  # untouched — extra key survives


def test_enabled_passes_non_dict_structured_content_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = ToolResult(content=[])  # no structured_content
    out = _drive(result, enabled=True, monkeypatch=monkeypatch)
    assert out is result


def test_enabled_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    once = _drive(_full_collection(), enabled=True, monkeypatch=monkeypatch)
    twice = _drive(once, enabled=True, monkeypatch=monkeypatch)
    assert twice.structured_content == once.structured_content


# --- integration: real app + gateway, flag on ------------------------------


def _gateway_app() -> FastMCP:
    """A minimal app: Compact outermost, the real tools.invoke gateway, and one list tool."""
    app = FastMCP(name="probe")
    app.add_middleware(CompactResponseMiddleware())  # first == outermost
    register_gateway(app, resolver=ProviderResolver({}))

    @app.tool(name="images.list")
    async def images_list() -> ToolResponse:
        rows = [ToolResponse.success("img-0", "registered", data={"name": "n0"})]
        return ToolResponse.collection("images", "ok", rows)

    return app


def test_integration_gateway_routed_response_compacted(monkeypatch: pytest.MonkeyPatch) -> None:
    # tools.invoke re-enters the chain (run_middleware=True): inner + outer compaction pass.
    monkeypatch.setenv("KDIVE_COMPACT_RESPONSES", "on")

    async def _run() -> dict[str, Any]:
        async with Client(_gateway_app()) as client:
            res = await client.call_tool("tools.invoke", {"name": "images.list", "arguments": {}})
            return res.structured_content

    sc = asyncio.run(_run())
    assert "error_category" not in sc and "refs" not in sc
    row = sc["items"][0]
    assert set(row) == {"object_id", "status", "data"}  # inner row compacted too
    assert row["data"] == {"name": "n0"}


def test_integration_synthesized_failure_envelope_compacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An unknown tool makes tools.invoke synthesize a full configuration_error envelope; the
    # outermost middleware must compact it (proving it wraps handler/downstream-synthesized
    # results).
    monkeypatch.setenv("KDIVE_COMPACT_RESPONSES", "on")

    async def _run() -> dict[str, Any]:
        async with Client(_gateway_app()) as client:
            res = await client.call_tool("tools.invoke", {"name": "nope.missing", "arguments": {}})
            return res.structured_content

    sc = asyncio.run(_run())
    assert sc["error_category"] == "configuration_error"
    assert sc["retryable"] is False and sc["detail"]  # failure fields kept
    assert "refs" not in sc and "items" not in sc  # empty defaults compacted away
