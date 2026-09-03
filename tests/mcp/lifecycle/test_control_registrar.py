"""Characterization tests for the control FastMCP registrar."""

from __future__ import annotations

import asyncio
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastmcp import FastMCP
from fastmcp.tools.function_tool import FunctionTool
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import RUNS
from kdive.domain.capacity.state import ExternalBootActivationState, RunState
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.lifecycle.records import Run
from kdive.mcp.schema.schema_advertising import registered_tools
from kdive.mcp.tools.lifecycle.control import registrar
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.authz.rbac import Role
from tests.mcp.lifecycle import runs_support
from tests.services.external_boot.conftest import seed_activation


def _register_tools() -> tuple[dict[str, FunctionTool], AsyncConnectionPool, ProviderResolver]:
    app = FastMCP("control-registrar-test")
    pool = cast(AsyncConnectionPool, object())
    resolver = cast(ProviderResolver, object())
    registrar.register(app, pool, resolver=resolver)
    tools = {tool.name: cast(FunctionTool, tool) for tool in registered_tools(app)}
    return tools, pool, resolver


def _destructive_hint(tool: FunctionTool) -> bool | None:
    annotations = tool.annotations
    return None if annotations is None else annotations.destructiveHint


def _read_only_hint(tool: FunctionTool) -> bool | None:
    annotations = tool.annotations
    return None if annotations is None else annotations.readOnlyHint


def test_register_publishes_control_tool_contracts() -> None:
    tools, _pool, _resolver = _register_tools()

    assert list(tools) == [
        "control.power",
        "control.force_crash",
        "control.diagnostic_sysrq",
        "control.watch_for_crash",
        "control.capture_traffic",
    ]
    assert _destructive_hint(tools["control.force_crash"]) is True
    assert all(
        _destructive_hint(tool) is False
        for name, tool in tools.items()
        if name != "control.force_crash"
    )
    assert all(_read_only_hint(tool) is False for tool in tools.values())
    assert all((tool.meta or {}) == {"maturity": "implemented"} for tool in tools.values())

    assert list(tools["control.power"].parameters["properties"]) == [
        "system_id",
        "action",
        "idempotency_key",
    ]
    assert tools["control.power"].parameters["required"] == ["system_id", "action"]
    assert tools["control.power"].parameters["properties"]["idempotency_key"]["default"] is None

    assert list(tools["control.force_crash"].parameters["properties"]) == [
        "system_id",
        "idempotency_key",
    ]
    assert tools["control.force_crash"].parameters["required"] == ["system_id"]
    assert (
        tools["control.force_crash"].parameters["properties"]["idempotency_key"]["default"] is None
    )

    assert list(tools["control.diagnostic_sysrq"].parameters["properties"]) == [
        "system_id",
        "command",
        "idempotency_key",
    ]
    assert tools["control.diagnostic_sysrq"].parameters["required"] == ["system_id", "command"]
    assert (
        tools["control.diagnostic_sysrq"].parameters["properties"]["idempotency_key"]["default"]
        is None
    )

    assert list(tools["control.watch_for_crash"].parameters["properties"]) == [
        "system_id",
        "deadline_s",
        "idempotency_key",
    ]
    assert tools["control.watch_for_crash"].parameters["required"] == ["system_id"]
    assert (
        tools["control.watch_for_crash"].parameters["properties"]["deadline_s"]["default"] == 60.0
    )
    assert (
        tools["control.watch_for_crash"].parameters["properties"]["idempotency_key"]["default"]
        is None
    )

    capture = tools["control.capture_traffic"].parameters
    assert list(capture["properties"]) == [
        "run_id",
        "duration_s",
        "max_bytes",
        "snaplen",
        "capture_filter",
        "idempotency_key",
    ]
    assert capture["required"] == ["run_id"]
    assert capture["properties"]["duration_s"] == {
        "default": 30,
        "description": "Capture window in seconds (1-300); the job auto-stops when it elapses. "
        "Cancel early with jobs.cancel.",
        "maximum": 300,
        "minimum": 1,
        "type": "integer",
    }
    assert capture["properties"]["max_bytes"] == {
        "default": 67108864,
        "description": "Stop early once the pcap reaches this many bytes (1048576-536870912).",
        "maximum": 536870912,
        "minimum": 1048576,
        "type": "integer",
    }
    assert capture["properties"]["snaplen"] == {
        "default": 128,
        "description": "Bytes captured per packet (1-262144); the default 128 captures headers "
        "only. Raise it to keep payloads.",
        "maximum": 262144,
        "minimum": 1,
        "type": "integer",
    }
    assert capture["properties"]["capture_filter"]["default"] is None
    assert capture["properties"]["idempotency_key"]["default"] is None


def test_registered_wrappers_delegate_to_control_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = object()
    calls: list[tuple[str, object, object, object | None, dict[str, object]]] = []
    sentinels = {name: object() for name in ("power", "force_crash", "sysrq", "watch", "capture")}

    def _current_context() -> object:
        return ctx

    async def _power(pool: object, actual_ctx: object, **kwargs: object) -> object:
        calls.append(("power", pool, actual_ctx, None, kwargs))
        return sentinels["power"]

    async def _force_crash(pool: object, actual_ctx: object, **kwargs: object) -> object:
        calls.append(("force_crash", pool, actual_ctx, kwargs.pop("resolver"), kwargs))
        return sentinels["force_crash"]

    async def _sysrq(pool: object, actual_ctx: object, **kwargs: object) -> object:
        calls.append(("sysrq", pool, actual_ctx, kwargs.pop("resolver"), kwargs))
        return sentinels["sysrq"]

    async def _watch(pool: object, actual_ctx: object, **kwargs: object) -> object:
        calls.append(("watch", pool, actual_ctx, kwargs.pop("resolver"), kwargs))
        return sentinels["watch"]

    async def _capture(pool: object, actual_ctx: object, **kwargs: object) -> object:
        calls.append(("capture", pool, actual_ctx, kwargs.pop("resolver"), kwargs))
        return sentinels["capture"]

    monkeypatch.setattr(registrar, "current_context", _current_context)
    monkeypatch.setattr(registrar, "power_system", _power)
    monkeypatch.setattr(registrar, "force_crash_system", _force_crash)
    monkeypatch.setattr(registrar, "diagnostic_sysrq_system", _sysrq)
    monkeypatch.setattr(registrar, "watch_for_crash_system", _watch)
    monkeypatch.setattr(registrar, "capture_traffic_system", _capture)
    tools, pool, resolver = _register_tools()

    async def _run() -> None:
        assert (
            await tools["control.power"].fn("power-system", "cycle", "power-idem")
            is sentinels["power"]
        )
        assert (
            await tools["control.force_crash"].fn("crash-system", "crash-idem")
            is sentinels["force_crash"]
        )
        assert (
            await tools["control.diagnostic_sysrq"].fn("sysrq-system", "t", "sysrq-idem")
            is sentinels["sysrq"]
        )
        assert (
            await tools["control.watch_for_crash"].fn("watch-system", 17.5, "watch-idem")
            is sentinels["watch"]
        )
        assert (
            await tools["control.capture_traffic"].fn(
                "capture-run", 42, 8192, 512, "tcp port 443", "capture-idem"
            )
            is sentinels["capture"]
        )

    asyncio.run(_run())

    assert calls == [
        (
            "power",
            pool,
            ctx,
            None,
            {"system_id": "power-system", "action": "cycle", "idempotency_key": "power-idem"},
        ),
        (
            "force_crash",
            pool,
            ctx,
            resolver,
            {"system_id": "crash-system", "idempotency_key": "crash-idem"},
        ),
        (
            "sysrq",
            pool,
            ctx,
            resolver,
            {
                "system_id": "sysrq-system",
                "command": "t",
                "idempotency_key": "sysrq-idem",
            },
        ),
        (
            "watch",
            pool,
            ctx,
            resolver,
            {"system_id": "watch-system", "deadline_s": 17.5, "idempotency_key": "watch-idem"},
        ),
        (
            "capture",
            pool,
            ctx,
            resolver,
            {
                "run_id": "capture-run",
                "duration_s": 42,
                "max_bytes": 8192,
                "snaplen": 512,
                "capture_filter": "tcp port 443",
                "idempotency_key": "capture-idem",
            },
        ),
    ]


def test_register_dispatches_to_focused_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object, object, object | None]] = []

    def _power(app: object, pool: object) -> None:
        calls.append(("power", app, pool, None))

    def _force_crash(app: object, pool: object, resolver: object) -> None:
        calls.append(("force_crash", app, pool, resolver))

    def _sysrq(app: object, pool: object, resolver: object) -> None:
        calls.append(("diagnostic_sysrq", app, pool, resolver))

    def _watch(app: object, pool: object, resolver: object) -> None:
        calls.append(("watch_for_crash", app, pool, resolver))

    def _capture(app: object, pool: object, resolver: object) -> None:
        calls.append(("capture_traffic", app, pool, resolver))

    monkeypatch.setattr(registrar, "_register_control_power", _power, raising=False)
    monkeypatch.setattr(registrar, "_register_control_force_crash", _force_crash, raising=False)
    monkeypatch.setattr(registrar, "_register_control_diagnostic_sysrq", _sysrq, raising=False)
    monkeypatch.setattr(registrar, "_register_control_watch_for_crash", _watch, raising=False)
    monkeypatch.setattr(registrar, "_register_control_capture_traffic", _capture, raising=False)
    app = FastMCP("control-registrar-dispatch-test")
    pool = cast(AsyncConnectionPool, object())
    resolver = cast(ProviderResolver, object())

    registrar.register(app, pool, resolver=resolver)

    assert calls == [
        ("power", app, pool, None),
        ("force_crash", app, pool, resolver),
        ("diagnostic_sysrq", app, pool, resolver),
        ("watch_for_crash", app, pool, resolver),
        ("capture_traffic", app, pool, resolver),
    ]


def test_power_is_denied_while_an_external_boot_activation_restricts_the_system(
    migrated_url: str,
) -> None:
    """ADR-0583: `control.power` is in no admitted row, so any restriction refuses it."""

    async def scenario() -> None:
        async with runs_support.pool(migrated_url) as pool:
            sys_id = await runs_support.seed_system(pool)
            inv_id = await runs_support.seed_investigation(pool)
            async with pool.connection() as conn:
                run = await RUNS.insert(
                    conn,
                    Run(
                        id=uuid4(),
                        created_at=runs_support.TEST_DT,
                        updated_at=runs_support.TEST_DT,
                        principal="user-1",
                        project="proj",
                        investigation_id=UUID(inv_id),
                        system_id=UUID(sys_id),
                        target_kind=ResourceKind.LOCAL_LIBVIRT,
                        state=RunState.SUCCEEDED,
                        build_profile={},
                    ),
                )
                await seed_activation(
                    conn,
                    state=ExternalBootActivationState.ACTIVE,
                    system_id=UUID(sys_id),
                    run_id=run.id,
                )
            resp = await registrar.power_system(
                pool, runs_support.ctx(Role.CONTRIBUTOR), system_id=sys_id, action="off"
            )
            assert resp.error_category == "conflict", resp.model_dump()
            assert resp.object_id == sys_id
            assert resp.suggested_next_actions == ["runs.get", "runs.release_external_boot"]
            async with pool.connection() as conn:
                cur = await conn.execute("SELECT count(*) FROM jobs WHERE kind = 'power'")
                row = await cur.fetchone()
                assert row is not None and row[0] == 0

    asyncio.run(scenario())
