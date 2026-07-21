"""Registration tests for vmcore and postmortem FastMCP wrappers."""

from __future__ import annotations

import asyncio
from typing import ClassVar, cast

import pytest
from fastmcp import FastMCP
from fastmcp.tools.function_tool import FunctionTool
from psycopg_pool import AsyncConnectionPool

from kdive.domain.capture import CaptureMethod
from kdive.mcp.responses import ToolResponse
from kdive.mcp.schema.schema_advertising import registered_tools
from kdive.mcp.tools.lifecycle.vmcore import registrar
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.secrets.secret_registry import SecretRegistry

type _Call = tuple[str, object, object, dict[str, object]]


class _FakeVmcoreHandlers:
    created: ClassVar[list[_FakeVmcoreHandlers]] = []

    def __init__(self, *, resolver: ProviderResolver, secret_registry: SecretRegistry) -> None:
        self.resolver = resolver
        self.secret_registry = secret_registry
        self.calls: list[_Call] = []
        self.created.append(self)

    async def fetch_vmcore(
        self,
        pool: AsyncConnectionPool,
        ctx: object,
        *,
        run_id: str,
        method: CaptureMethod | None,
        idempotency_key: str | None,
    ) -> ToolResponse:
        self.calls.append(
            (
                "fetch_vmcore",
                pool,
                ctx,
                {"run_id": run_id, "method": method, "idempotency_key": idempotency_key},
            )
        )
        return ToolResponse.success("fetch", "ok")

    async def postmortem_crash(
        self,
        pool: AsyncConnectionPool,
        ctx: object,
        *,
        run_id: str,
        commands: list[str],
    ) -> ToolResponse:
        self.calls.append(
            (
                "postmortem_crash",
                pool,
                ctx,
                {"run_id": run_id, "commands": commands},
            )
        )
        return ToolResponse.success("crash", "ok")

    async def postmortem_triage(
        self,
        pool: AsyncConnectionPool,
        ctx: object,
        *,
        run_id: str,
    ) -> ToolResponse:
        self.calls.append(("postmortem_triage", pool, ctx, {"run_id": run_id}))
        return ToolResponse.success("triage", "ok")


def _register_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, FunctionTool], object, AsyncConnectionPool]:
    _FakeVmcoreHandlers.created.clear()
    ctx = object()

    def _current_context() -> object:
        return ctx

    monkeypatch.setattr(registrar, "VmcoreHandlers", _FakeVmcoreHandlers)
    monkeypatch.setattr(registrar, "current_context", _current_context)
    app = FastMCP("vmcore-registrar-test")
    pool = cast(AsyncConnectionPool, object())
    resolver = cast(ProviderResolver, object())
    registrar.register(app, pool, resolver=resolver, secret_registry=SecretRegistry())
    tools = {tool.name: cast(FunctionTool, tool) for tool in registered_tools(app)}
    return tools, ctx, pool


def _read_only_hint(tool: FunctionTool) -> bool | None:
    annotations = tool.annotations
    return None if annotations is None else annotations.readOnlyHint


def _property_descriptions(tool: FunctionTool) -> dict[str, str]:
    properties = tool.parameters["properties"]
    return {
        name: value["description"]
        for name, value in properties.items()
        if isinstance(value, dict) and isinstance(value.get("description"), str)
    }


def test_register_publishes_vmcore_and_postmortem_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, _ctx, _pool = _register_tools(monkeypatch)

    assert set(tools) == {
        "vmcore.fetch",
        "vmcore.list",
        "postmortem.crash",
        "postmortem.triage",
    }
    assert _read_only_hint(tools["vmcore.fetch"]) is False
    assert _read_only_hint(tools["vmcore.list"]) is True
    assert _read_only_hint(tools["postmortem.crash"]) is True
    assert _read_only_hint(tools["postmortem.triage"]) is True
    assert all((tool.meta or {}) == {"maturity": "implemented"} for tool in tools.values())

    fetch_descriptions = _property_descriptions(tools["vmcore.fetch"])
    assert fetch_descriptions["run_id"] == "The crashed Run whose vmcore to capture."
    assert "Omit to resolve the System profile's method" in fetch_descriptions["method"]
    assert fetch_descriptions["idempotency_key"] == (
        "Replay-safe key; a repeated key returns the prior envelope."
    )
    assert set(tools["vmcore.fetch"].parameters["$defs"]["CaptureMethod"]["enum"]) == {
        method.value for method in CaptureMethod
    }

    assert _property_descriptions(tools["vmcore.list"]) == {
        "run_id": "The Run whose redacted vmcore artifacts to list."
    }
    crash_descriptions = _property_descriptions(tools["postmortem.crash"])
    assert crash_descriptions["run_id"] == "The Run whose captured core to analyze."
    # The commands Field enumerates the allowlisted verbs and names the rejection-detail
    # contract instead of the old opaque "allowlisted read-only verbs" (#1361 F3).
    commands_desc = crash_descriptions["commands"]
    assert "bt" in commands_desc and "log" in commands_desc and "struct" in commands_desc
    assert "detail names the offending command" in commands_desc
    assert _property_descriptions(tools["postmortem.triage"]) == {
        "run_id": "The Run whose captured core to triage."
    }


def test_registered_wrappers_delegate_to_vmcore_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    list_calls: list[tuple[object, object, str]] = []

    async def _list_vmcores(pool: AsyncConnectionPool, ctx: object, *, run_id: str) -> ToolResponse:
        list_calls.append((pool, ctx, run_id))
        return ToolResponse.success("list", "ok")

    monkeypatch.setattr(registrar, "list_vmcores", _list_vmcores)
    tools, ctx, pool = _register_tools(monkeypatch)

    async def _run() -> None:
        await tools["vmcore.fetch"].fn(
            "run-1",
            method=CaptureMethod.KDUMP,
            idempotency_key="idem-1",
        )
        await tools["vmcore.list"].fn("run-2")
        await tools["postmortem.crash"].fn("run-3", ["sys", "log"])
        await tools["postmortem.triage"].fn("run-4")

    asyncio.run(_run())

    handlers = _FakeVmcoreHandlers.created[0]
    assert handlers.calls == [
        (
            "fetch_vmcore",
            pool,
            ctx,
            {
                "run_id": "run-1",
                "method": CaptureMethod.KDUMP,
                "idempotency_key": "idem-1",
            },
        ),
        (
            "postmortem_crash",
            pool,
            ctx,
            {"run_id": "run-3", "commands": ["sys", "log"]},
        ),
        ("postmortem_triage", pool, ctx, {"run_id": "run-4"}),
    ]
    assert list_calls == [(pool, ctx, "run-2")]
