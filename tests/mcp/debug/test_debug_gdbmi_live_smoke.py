"""Live gdb-MI smoke for the promoted ``debug.*`` tool surface.

The deterministic fake-controller tests cover edge cases. This ``live_vm`` suite exercises the
real gdb/MI process against a real preserved local-libvirt gdbstub: attach, disassemble, hardware
watchpoint set/list/clear, module listing, and optional module-symbol loading when the operator
provides a loaded module fixture.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import os
import shutil
import socket
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from fastmcp import Client, FastMCP
from psycopg_pool import AsyncConnectionPool

import kdive.mcp.tools.debug.operations.breakpoints as ops_breakpoints
import kdive.mcp.tools.debug.operations.execution as ops_execution
import kdive.mcp.tools.debug.operations.memory as ops_memory
import kdive.mcp.tools.debug.operations.modules as ops_modules
import kdive.mcp.tools.debug.operations.registrar as debug_ops_registrar
import kdive.mcp.tools.debug.operations.stack as ops_stack
import kdive.mcp.tools.debug.operations.watchpoints as ops_watchpoints
from kdive.domain.capacity.state import SystemState
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.debug.operations.runtime import DebugEngineRuntime
from kdive.mcp.tools.debug.sessions import lifecycle as debug_tools
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.core.resolver import ProviderBinding
from kdive.providers.local_libvirt.lifecycle.connect import LocalLibvirtConnect
from kdive.providers.local_libvirt.lifecycle.xml import render_domain_xml
from kdive.providers.ports.debug import GdbMiAttachment
from kdive.providers.shared.debug_common.gdbmi.core.engine import GdbMiEngine
from kdive.providers.shared.debug_common.gdbmi.policy.debuginfo import ModuleDebuginfo
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.testing.live_vm import boot_gdbstub_domain, create_overlay
from tests.live_vm import (
    require_live_vm_bzimage,
    require_live_vm_throwaway,
    require_live_vm_vmlinux,
)
from tests.mcp.debug.live_support import render_panicking_domain
from tests.mcp.debug.session_support import (
    PROFILE,
    PROFILE_POLICY,
    granted_allocation,
    request_context,
    seed_run,
    seed_system,
)
from tests.mcp.debug.session_support import (
    pool as open_pool,
)
from tests.mcp.systems_support import provider_resolver


@dataclass(frozen=True, slots=True)
class _ModuleFixture:
    name: str
    path: Path


@dataclass(frozen=True, slots=True)
class _LiveDebugSession:
    client: Client[Any]
    pool: AsyncConnectionPool
    session_id: str


@dataclass(frozen=True, slots=True)
class _LiveDebugSurface:
    """Shared real FastMCP/runtime/session setup for the two live gdb-MI proofs."""

    migrated_url: str
    monkeypatch: pytest.MonkeyPatch
    transcript_dir: Path

    @contextlib.asynccontextmanager
    async def session(
        self,
        *,
        vmlinux: Path,
        module_fixture: _ModuleFixture | None = None,
        boot_result: dict[str, object] | None = None,
    ) -> AsyncIterator[_LiveDebugSession]:
        engine = GdbMiEngine(module_debuginfo_resolver=_module_resolver(module_fixture))
        runtime = DebugEngineRuntime(
            engine=engine,
            attach=_attach_with_vmlinux(engine, vmlinux),
            transcript_dir=self.transcript_dir,
        )
        runtime_resolver = _FixedDebugRuntimeResolver(runtime)
        async with open_pool(self.migrated_url) as pool:
            session_id = await _start_live_session(pool, runtime_resolver, boot_result=boot_result)
            try:
                async with _debug_client(pool, runtime_resolver, self.monkeypatch) as client:
                    yield _LiveDebugSession(client=client, pool=pool, session_id=session_id)
            finally:
                await _end_live_session(pool, runtime_resolver, session_id)


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


@pytest.fixture
def live_debug_surface(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> _LiveDebugSurface:
    return _LiveDebugSurface(
        migrated_url=migrated_url,
        monkeypatch=monkeypatch,
        transcript_dir=tmp_path / "gdbmi-transcripts",
    )


@pytest.mark.live_vm
@pytest.mark.live_vm_throwaway
def test_live_vm_gdbmi_promoted_ops_smoke(  # pragma: no cover - live_vm
    live_debug_surface: _LiveDebugSurface,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    contract = require_live_vm_bzimage()
    vmlinux = require_live_vm_vmlinux().vmlinux
    _require_live_debug_dependencies()

    monkeypatch.setenv("KDIVE_LIBVIRT_URI", contract.libvirt_uri)
    disk = tmp_path / "garbage.qcow2"
    console = tmp_path / "console.log"
    console.write_text("")
    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", str(disk), "1G"], check=True, capture_output=True
    )

    final_xml = render_panicking_domain(bzimage=str(contract.bzimage), disk=disk, console=console)
    module_fixture = _optional_module_fixture()
    with boot_gdbstub_domain(
        final_xml,
        uri=contract.libvirt_uri,
        wait_for="panic",
        console_log=console,
    ):
        asyncio.run(_drive_gdbmi_smoke(live_debug_surface, vmlinux, module_fixture))


@pytest.mark.live_vm
@pytest.mark.live_vm_throwaway
def test_live_vm_debug_advance_modes(  # pragma: no cover - live_vm
    live_debug_surface: _LiveDebugSurface,
) -> None:
    rootfs_contract = require_live_vm_throwaway("qemu:///session", session_required=True)
    bzimage_contract = require_live_vm_bzimage()
    vmlinux = require_live_vm_vmlinux().vmlinux
    _require_live_debug_dependencies()
    assert bzimage_contract.libvirt_uri == rootfs_contract.libvirt_uri
    live_debug_surface.monkeypatch.setenv("KDIVE_LIBVIRT_URI", rootfs_contract.libvirt_uri)

    gdb_port, ssh_port = _ephemeral_port_pair()
    with _rootfs_overlay(rootfs_contract.rootfs) as overlay:
        xml = _render_stepping_domain(
            disk=overlay,
            bzimage=bzimage_contract.bzimage,
            gdb_port=gdb_port,
            ssh_port=ssh_port,
        )
        with boot_gdbstub_domain(
            xml,
            uri=rootfs_contract.libvirt_uri,
            wait_for="ssh",
            ssh_port=ssh_port,
            wait_timeout_s=180.0,
        ):
            asyncio.run(_drive_advance_modes(live_debug_surface, vmlinux, ssh_port))


def test_continue_to_vfs_read_closes_ssh_trigger_on_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        banner_received: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        disconnected = asyncio.Event()

        async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                banner_received.set_result(await reader.readline())
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
                disconnected.set()

        server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
        socket_address = server.sockets[0].getsockname()
        ssh_port = int(socket_address[1])

        async def fail_call(
            _client: Client[Any], tool: str, arguments: dict[str, object]
        ) -> ToolResponse:
            assert tool == "debug.continue"
            assert arguments["session_id"] == "session-x"
            banner = await asyncio.wait_for(banner_received, timeout=1.0)
            assert banner == b"SSH-2.0-kdive-live-test\r\n"
            raise RuntimeError("continue failed")

        monkeypatch.setattr(sys.modules[__name__], "_call_tool", fail_call)
        try:
            with pytest.raises(RuntimeError, match="continue failed"):
                await _continue_to_vfs_read(cast(Any, object()), "session-x", ssh_port)
            await asyncio.wait_for(disconnected.wait(), timeout=1.0)
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(exercise())


def test_continue_to_vfs_read_waits_for_underlying_call_after_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        banner_received: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        disconnected = asyncio.Event()
        call_started = asyncio.Event()
        allow_work_to_finish = asyncio.Event()
        work_finished = asyncio.Event()

        async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                banner_received.set_result(await reader.readline())
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
                disconnected.set()

        async def underlying_work() -> ToolResponse:
            await allow_work_to_finish.wait()
            work_finished.set()
            return ToolResponse.success("session-x", "stopped")

        server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
        ssh_port = int(server.sockets[0].getsockname()[1])
        work_task = asyncio.create_task(underlying_work())

        async def call_with_cancellation_insensitive_work(
            _client: Client[Any], _tool: str, _arguments: dict[str, object]
        ) -> ToolResponse:
            await asyncio.wait_for(banner_received, timeout=1.0)
            call_started.set()
            return await asyncio.shield(work_task)

        monkeypatch.setattr(
            sys.modules[__name__], "_call_tool", call_with_cancellation_insensitive_work
        )
        continue_task = asyncio.create_task(
            _continue_to_vfs_read(cast(Any, object()), "session-x", ssh_port)
        )
        try:
            await asyncio.wait_for(call_started.wait(), timeout=1.0)
            continue_task.cancel()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(disconnected.wait(), timeout=0.05)
            assert not continue_task.done(), "cleanup returned while underlying work was running"
            allow_work_to_finish.set()
            with pytest.raises(asyncio.CancelledError):
                await continue_task
            assert work_finished.is_set()
            await asyncio.wait_for(disconnected.wait(), timeout=1.0)
        finally:
            allow_work_to_finish.set()
            with contextlib.suppress(asyncio.CancelledError):
                await continue_task
            await work_task
            server.close()
            await server.wait_closed()

    asyncio.run(exercise())


async def _drive_gdbmi_smoke(
    surface: _LiveDebugSurface,
    vmlinux: Path,
    module_fixture: _ModuleFixture | None,
) -> None:
    async with surface.session(
        vmlinux=vmlinux,
        module_fixture=module_fixture,
        boot_result={"boot_outcome": "crashed_halted_live"},
    ) as live:
        session_id = live.session_id
        client = live.client
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
        listed = await _call_tool(client, "debug.list_watchpoints", {"session_id": session_id})
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
        await _load_module_symbols_when_configured(client, session_id, modules, module_fixture)


async def _drive_advance_modes(surface: _LiveDebugSurface, vmlinux: Path, ssh_port: int) -> None:
    async with surface.session(vmlinux=vmlinux) as live:
        for mode in ("into", "over", "instruction", "out"):
            await _exercise_advance_mode(live.client, live.session_id, mode, ssh_port)

        transitions = await _advance_audit_transitions(live.pool, live.session_id)
        assert sorted(transitions) == sorted(
            ["advance:into", "advance:over", "advance:instruction", "advance:out"]
        )


async def _exercise_advance_mode(
    client: Client[Any], session_id: str, mode: str, ssh_port: int
) -> None:
    breakpoint_number: str | None = None
    try:
        breakpoint = await _call_tool(
            client,
            "debug.set_breakpoint",
            {"session_id": session_id, "location": "vfs_read"},
        )
        assert breakpoint.status == "set", breakpoint
        breakpoint_number = str(breakpoint.data["number"])

        continued = await _continue_to_vfs_read(client, session_id, ssh_port)
        _assert_nonterminal_stop(continued)
        assert continued.data["reason"] == "breakpoint-hit", continued
        cleared = await _call_tool(
            client,
            "debug.clear_breakpoint",
            {"session_id": session_id, "number": breakpoint_number},
        )
        assert cleared.status == "cleared", cleared
        breakpoint_number = None

        before = await _read_instruction_pointer(client, session_id)
        advanced = await _call_tool(
            client,
            "debug.advance",
            {"session_id": session_id, "mode": mode, "timeout_sec": 30.0},
        )
        _assert_nonterminal_stop(advanced)
        assert advanced.error_category is None, advanced
        assert advanced.suggested_next_actions == [
            "debug.read_registers",
            "debug.backtrace",
            "debug.advance",
            "debug.continue",
        ]
        after = await _read_instruction_pointer(client, session_id)
        assert after != before, f"mode={mode} did not advance rip ({before} -> {after})"
        if mode == "out":
            assert advanced.data["reason"] == "function-finished", advanced
    finally:
        if breakpoint_number is not None:
            cleared = await _call_tool(
                client,
                "debug.clear_breakpoint",
                {"session_id": session_id, "number": breakpoint_number},
            )
            assert cleared.status == "cleared", cleared


async def _continue_to_vfs_read(
    client: Client[Any], session_id: str, ssh_port: int
) -> ToolResponse:
    """Resume with a queued SSH client read so ``vfs_read`` is not ambient-I/O dependent."""
    async with _queued_ssh_read_trigger(ssh_port):
        continue_task = asyncio.create_task(
            _call_tool(
                client,
                "debug.continue",
                {"session_id": session_id, "timeout_sec": 30.0},
            )
        )
        cancelled = False
        while True:
            try:
                response = await asyncio.shield(continue_task)
                break
            except asyncio.CancelledError:
                if continue_task.cancelled():
                    return await continue_task
                cancelled = True
                if continue_task.done():
                    response = continue_task.result()
                    break
        if cancelled:
            raise asyncio.CancelledError()
        return response


@contextlib.asynccontextmanager
async def _queued_ssh_read_trigger(ssh_port: int) -> AsyncIterator[None]:
    """Queue a client banner that makes guest sshd call read(2), holding the socket open."""
    _reader, writer = await asyncio.open_connection("127.0.0.1", ssh_port)
    try:
        writer.write(b"SSH-2.0-kdive-live-test\r\n")
        await writer.drain()
        yield
    finally:
        writer.close()
        await writer.wait_closed()


def _assert_nonterminal_stop(response: ToolResponse) -> None:
    assert response.status == "stopped", response
    assert response.data["timed_out"] is False, response
    reason = response.data.get("reason")
    assert isinstance(reason, str) and reason, response
    assert not reason.startswith("exited"), response


async def _read_instruction_pointer(client: Client[Any], session_id: str) -> str:
    response = await _call_tool(
        client,
        "debug.read_registers",
        {"session_id": session_id, "registers": ["rip"]},
    )
    assert response.status == "read", response
    instruction_pointer = response.data.get("rip")
    assert isinstance(instruction_pointer, str) and instruction_pointer, response
    return instruction_pointer


async def _advance_audit_transitions(pool: AsyncConnectionPool, session_id: str) -> list[str]:
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT transition FROM audit_log "
            "WHERE object_id = %s AND tool = 'debug.advance' ORDER BY transition",
            (session_id,),
        )
        return [str(row[0]) for row in await cursor.fetchall()]


async def _start_live_session(
    pool: AsyncConnectionPool,
    runtime_resolver: _FixedDebugRuntimeResolver,
    *,
    boot_result: dict[str, object] | None,
) -> str:
    alloc_id = await granted_allocation(pool)
    sys_id = await seed_system(pool, alloc_id, SystemState.READY)
    run_id = await seed_run(pool, sys_id, boot_result=boot_result)
    handlers = _session_handlers(runtime_resolver)
    resp = await handlers.start_session(pool, request_context(), run_id=run_id, transport="gdbstub")
    assert resp.status == "live", resp
    return resp.object_id


async def _end_live_session(
    pool: AsyncConnectionPool,
    runtime_resolver: _FixedDebugRuntimeResolver,
    session_id: str,
) -> None:
    handlers = _session_handlers(runtime_resolver)
    resp = await handlers.end_session(pool, request_context(), session_id)
    assert resp.status in {"detached", "already_detached"}, resp


def _session_handlers(
    runtime_resolver: _FixedDebugRuntimeResolver,
) -> debug_tools.DebugSessionHandlers:
    return debug_tools.DebugSessionHandlers.from_resolver(
        provider_resolver(
            connector=LocalLibvirtConnect.from_env(),
            profile_policy=PROFILE_POLICY,
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
        monkeypatch.setattr(module, "current_context", request_context)
    app: FastMCP = FastMCP(name="live-gdbmi-smoke")
    debug_ops_registrar.register(app, pool, cast(Any, runtime_resolver))
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


def _require_live_debug_dependencies() -> None:
    for tool in ("gdb", "qemu-img"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} unavailable")
    try:
        import libvirt  # noqa: F401, PLC0415  # operator-provided; presence gates the live boot
    except ImportError:
        pytest.skip("libvirt-python unavailable")


def _ephemeral_port_pair() -> tuple[int, int]:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as gdb_socket,
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as ssh_socket,
    ):
        gdb_socket.bind(("127.0.0.1", 0))
        ssh_socket.bind(("127.0.0.1", 0))
        gdb_port = int(gdb_socket.getsockname()[1])
        ssh_port = int(ssh_socket.getsockname()[1])
    assert gdb_port != ssh_port
    return gdb_port, ssh_port


@contextlib.contextmanager
def _rootfs_overlay(rootfs: Path) -> Iterator[Path]:
    overlay = rootfs.with_name(f"kdive-debug-{uuid4().hex}.qcow2")
    try:
        create_overlay(rootfs, overlay)
        yield overlay
    finally:
        overlay.unlink(missing_ok=True)


def _render_stepping_domain(*, disk: Path, bzimage: Path, gdb_port: int, ssh_port: int) -> str:
    data = copy.deepcopy(PROFILE)
    section = data["provider"]["local-libvirt"]
    section["rootfs"] = {"kind": "local", "path": str(disk)}
    section["debug"] = {"gdbstub": True}
    section.pop("crashkernel", None)
    profile = ProvisioningProfile.parse(data)
    rendered = render_domain_xml(
        uuid4(),
        profile,
        disk_path=str(disk),
        gdb_port=gdb_port,
        ssh_port=ssh_port,
        kernel_path=bzimage,
    )
    root = ET.fromstring(rendered)  # noqa: S314 - kdive-rendered, trusted
    name = root.find("name")
    cmdline = root.find("./os/cmdline")
    assert name is not None and cmdline is not None and cmdline.text is not None
    name.text = "kdive-x"  # matches the System row seeded by seed_system
    cmdline.text = f"{cmdline.text} nokaslr"
    return ET.tostring(root, encoding="unicode")
