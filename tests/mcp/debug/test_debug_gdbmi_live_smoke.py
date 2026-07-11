"""Live gdb-MI smoke for the promoted ``debug.*`` tool surface.

The deterministic fake-controller tests cover edge cases. This ``live_vm`` suite exercises the
real gdb/MI process against a real preserved local-libvirt gdbstub: attach, disassemble, hardware
watchpoint set/list/clear, module listing, and optional module-symbol loading when the operator
provides a loaded module fixture.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from fastmcp import Client, FastMCP
from psycopg_pool import AsyncConnectionPool

import kdive.mcp.tools.debug.operations.registrar as debug_ops_registrar
from kdive.domain.capacity.state import SystemState
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.debug import (
    sessions as debug_tools,
)
from kdive.mcp.tools.debug.operations import (
    DebugEngineRuntime,
)
from kdive.mcp.tools.debug.operations import (
    breakpoints as ops_breakpoints,
)
from kdive.mcp.tools.debug.operations import (
    execution as ops_execution,
)
from kdive.mcp.tools.debug.operations import (
    memory as ops_memory,
)
from kdive.mcp.tools.debug.operations import (
    modules as ops_modules,
)
from kdive.mcp.tools.debug.operations import (
    stack as ops_stack,
)
from kdive.mcp.tools.debug.operations import (
    watchpoints as ops_watchpoints,
)
from kdive.providers.core.resolver import ProviderBinding
from kdive.providers.local_libvirt.lifecycle.connect import LocalLibvirtConnect
from kdive.providers.ports.debug import GdbMiAttachment
from kdive.providers.shared.debug_common.gdbmi.debuginfo import ModuleDebuginfo
from kdive.providers.shared.debug_common.gdbmi.engine import GdbMiEngine
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.debug.test_debug_live_attach import _render_panicking_domain
from tests.mcp.debug.test_debug_tools import (
    _PROFILE_POLICY,
    _ctx,
    _granted_allocation,
    _pool,
    _seed_run,
    _seed_system,
)
from tests.mcp.systems_support import provider_resolver


@dataclass(frozen=True, slots=True)
class _ModuleFixture:
    name: str
    path: Path


class _FixedDebugRuntimeResolver:
    def __init__(self, runtime: DebugEngineRuntime) -> None:
        self._runtime = runtime

    async def runtime_for_session(
        self, _pool: AsyncConnectionPool, _session_id: object
    ) -> DebugEngineRuntime:
        return self._runtime

    def runtime_for_binding(
        self, _binding: ProviderBinding, *, object_id: str | None = None
    ) -> DebugEngineRuntime:
        del object_id
        return self._runtime


@pytest.mark.live_vm
def test_live_vm_gdbmi_promoted_ops_smoke(  # pragma: no cover - live_vm
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bzimage = _required_file("KDIVE_LIVE_VM_BZIMAGE", "early-panicking kernel image")
    vmlinux = _required_file("KDIVE_LIVE_VM_GDBMI_VMLINUX", "matching vmlinux")
    if shutil.which("gdb") is None:
        pytest.skip("gdb unavailable")
    try:
        import libvirt  # noqa: PLC0415  # operator-provided
    except ImportError:
        pytest.skip("libvirt-python unavailable")

    uri = os.environ.get("KDIVE_LIBVIRT_URI", "qemu:///session")
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", uri)
    disk = tmp_path / "garbage.qcow2"
    console = tmp_path / "console.log"
    console.write_text("")
    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", str(disk), "1G"], check=True, capture_output=True
    )

    final_xml = _render_panicking_domain(bzimage=str(bzimage), disk=disk, console=console)
    module_fixture = _optional_module_fixture()
    engine = GdbMiEngine(module_debuginfo_resolver=_module_resolver(module_fixture))
    runtime = DebugEngineRuntime(
        engine=engine,
        attach=_attach_with_vmlinux(engine, vmlinux),
        transcript_dir=tmp_path / "gdbmi-transcripts",
    )
    runtime_resolver = _FixedDebugRuntimeResolver(runtime)

    conn = libvirt.open(uri)
    dom = None
    try:
        dom = conn.createXML(final_xml, 0)
        assert _await_panic(console, deadline_s=30.0), "no early-boot panic"
        asyncio.run(_drive_gdbmi_smoke(migrated_url, runtime_resolver, module_fixture, monkeypatch))
    finally:
        if dom is not None:
            with contextlib.suppress(libvirt.libvirtError):
                dom.destroy()
        conn.close()


async def _drive_gdbmi_smoke(
    migrated_url: str,
    runtime_resolver: _FixedDebugRuntimeResolver,
    module_fixture: _ModuleFixture | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _pool(migrated_url) as pool:
        session_id = await _start_live_session(pool, runtime_resolver)
        try:
            async with _debug_client(pool, runtime_resolver, monkeypatch) as client:
                disasm = await _call_tool(
                    client,
                    "debug.disassemble",
                    {"session_id": session_id, "symbol": "panic", "instruction_count": 8},
                )
                assert disasm.status == "disassembled", disasm
                instruction_count = disasm.data["instruction_count"]
                assert isinstance(instruction_count, int)
                assert instruction_count > 0

                watch = await _call_tool(
                    client,
                    "debug.set_watchpoint",
                    {"session_id": session_id, "symbol": "jiffies_64", "byte_count": 8},
                )
                assert watch.status == "watching", watch
                listed = await _call_tool(
                    client, "debug.list_watchpoints", {"session_id": session_id}
                )
                assert listed.status == "listed", listed
                watchpoint_count = listed.data["count"]
                assert isinstance(watchpoint_count, int)
                assert watchpoint_count >= 1
                cleared = await _call_tool(
                    client,
                    "debug.clear_watchpoint",
                    {"session_id": session_id, "number": watch.data["number"]},
                )
                assert cleared.status == "cleared", cleared

                modules = await _call_tool(client, "debug.list_modules", {"session_id": session_id})
                assert modules.status == "listed", modules
                await _load_module_symbols_when_configured(
                    client, session_id, modules, module_fixture
                )
        finally:
            await _end_live_session(pool, runtime_resolver, session_id)


async def _start_live_session(
    pool: AsyncConnectionPool, runtime_resolver: _FixedDebugRuntimeResolver
) -> str:
    alloc_id = await _granted_allocation(pool)
    sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
    run_id = await _seed_run(pool, sys_id, boot_result={"boot_outcome": "crashed_halted_live"})
    handlers = _session_handlers(runtime_resolver)
    resp = await handlers.start_session(pool, _ctx(), run_id=run_id, transport="gdbstub")
    assert resp.status == "live", resp
    return resp.object_id


async def _end_live_session(
    pool: AsyncConnectionPool,
    runtime_resolver: _FixedDebugRuntimeResolver,
    session_id: str,
) -> None:
    handlers = _session_handlers(runtime_resolver)
    resp = await handlers.end_session(pool, _ctx(), session_id)
    assert resp.status in {"detached", "already_detached"}, resp


def _session_handlers(
    runtime_resolver: _FixedDebugRuntimeResolver,
) -> debug_tools.DebugSessionHandlers:
    return debug_tools.DebugSessionHandlers.from_resolver(
        provider_resolver(
            connector=LocalLibvirtConnect.from_env(),
            profile_policy=_PROFILE_POLICY,
            supported_debug_transports=frozenset({"gdbstub"}),
        ),
        runtime_resolver=cast(Any, runtime_resolver),
        secret_registry=SecretRegistry(),
    )


@contextlib.asynccontextmanager
async def _debug_client(
    pool: AsyncConnectionPool,
    runtime_resolver: _FixedDebugRuntimeResolver,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    for module in (
        ops_breakpoints,
        ops_memory,
        ops_execution,
        ops_stack,
        ops_watchpoints,
        ops_modules,
    ):
        monkeypatch.setattr(module, "current_context", _ctx)
    app: FastMCP = FastMCP(name="live-gdbmi-smoke")
    debug_ops_registrar._register_debug_ops(app, pool, cast(Any, runtime_resolver))
    async with Client(app) as client:
        yield client


async def _call_tool(client: Client[Any], tool: str, arguments: dict[str, object]) -> ToolResponse:
    result = await client.call_tool(tool, arguments, raise_on_error=False)
    assert result.structured_content is not None
    return ToolResponse.model_validate(result.structured_content)


def _attach_with_vmlinux(engine: GdbMiEngine, vmlinux: Path) -> Any:
    def attach(*, host: str, port: int, run_id: str, transcript_path: Path) -> GdbMiAttachment:
        return engine.attach(
            host=host,
            port=port,
            vmlinux_path=vmlinux,
            transcript_path=transcript_path,
            run_id=run_id,
        )

    return attach


async def _load_module_symbols_when_configured(
    client: Client[Any],
    session_id: str,
    modules: ToolResponse,
    fixture: _ModuleFixture | None,
) -> None:
    if fixture is None:
        return
    module_rows = cast(list[dict[str, Any]], modules.data["modules"])
    row = next((item for item in module_rows if item.get("name") == fixture.name), None)
    if row is None:
        pytest.fail(f"{fixture.name} is configured but is not loaded in the live guest")
    loaded = await _call_tool(
        client,
        "debug.load_module_symbols",
        {
            "session_id": session_id,
            "module": fixture.name,
            "expected_base": row["base_address"],
        },
    )
    assert loaded.status == "loaded", loaded
    assert loaded.data["module"] == fixture.name
    assert loaded.data["symbols_loaded"] is True


def _module_resolver(fixture: _ModuleFixture | None) -> Any:
    def resolve(_run_id: str, module: str) -> ModuleDebuginfo:
        if fixture is None or module != fixture.name:
            raise AssertionError(f"unexpected module-symbol fixture request: {module}")
        return ModuleDebuginfo(path=fixture.path, srcversion=None, build_id=None)

    return resolve


def _optional_module_fixture() -> _ModuleFixture | None:
    raw_path = os.environ.get("KDIVE_LIVE_VM_GDBMI_MODULE_KO")
    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    if not path.is_file():
        pytest.skip("KDIVE_LIVE_VM_GDBMI_MODULE_KO is set but is not a file")
    name = os.environ.get("KDIVE_LIVE_VM_GDBMI_MODULE_NAME") or path.stem.replace("-", "_")
    return _ModuleFixture(name=name, path=path)


def _required_file(name: str, description: str) -> Path:
    raw = os.environ.get(name)
    if not raw:
        pytest.skip(f"{name} ({description}) unavailable")
    path = Path(raw).expanduser()
    if not path.is_file():
        pytest.skip(f"{name} ({description}) is not a file")
    return path


def _await_panic(console: Path, *, deadline_s: float) -> bool:
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if "Kernel panic" in console.read_text(errors="replace"):
            return True
        time.sleep(0.5)
    return False
