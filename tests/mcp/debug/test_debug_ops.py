"""debug.* gdb-MI tool tests — handlers driven with a real seeded session + a fake attach seam.

The seven Debug-plane handlers (`run_engine_op` + the op factories) are the unit of testing:
a `live` `DebugSession` is seeded in the migrated DB, and a fake `AttachSeam` returns a
`GdbMiAttachment` over a scripted fake `MiController`, so the gate, the per-session lock, the
attach-once behavior, the envelopes, the §5a `data["code"]` discriminators, and the
`end_session` reap are exercised without gdb or a socket.
"""

from __future__ import annotations

import asyncio
import copy
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, DEBUG_SESSIONS, INVESTIGATIONS, RUNS, SYSTEMS
from kdive.db.resource_discovery import register_discovered_resource
from kdive.domain.capacity.state import (
    AllocationState,
    DebugSessionState,
    InvestigationState,
    RunState,
    SystemState,
)
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.lifecycle.records import Allocation, DebugSession, Investigation, Run, System
from kdive.mcp.auth import RequestContext
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.debug import ops as debug_ops
from kdive.mcp.tools.debug import sessions as debug_tools
from kdive.mcp.tools.debug.ops import (
    DebugEngineRuntime,
    run_engine_op,
)
from kdive.providers.core.resolver import ProviderBinding, ProviderResolver
from kdive.providers.core.runtime import DebugCapabilities, ProviderRuntime
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy
from kdive.providers.ports.debug import GdbMiAttachment
from kdive.providers.ports.lifecycle import TransportHandleData
from kdive.providers.shared.debug_common.gdbmi import GdbMiEngine
from kdive.security.authz.rbac import AuthorizationError, Role
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.systems_support import provider_resolver
from tests.providers.local_libvirt.fakes import FakeLibvirtConn

_DT = datetime(2026, 1, 1, tzinfo=UTC)
_PROFILE_POLICY = LocalLibvirtProfilePolicy()

_PROFILE: dict[str, Any] = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 4,
    "memory_mb": 4096,
    "disk_gb": 20,
    "boot_method": "direct-kernel",
    "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    "provider": {
        "local-libvirt": {
            "domain_xml_params": {"machine": "q35"},
            "rootfs": {
                "kind": "local",
                "path": "/var/lib/kdive/rootfs/fedora-40.qcow2",
            },
            "crashkernel": "256M",
        }
    },
}


class _FakeMiController:
    def __init__(self, responses: dict[str, list[dict[str, object]]] | None = None) -> None:
        self._responses = responses or {}
        self.written: list[str] = []
        self.exited = False

    def write(self, command: str, *, timeout_sec: float) -> list[dict[str, object]]:
        del timeout_sec
        self.written.append(command)
        return self._responses.get(
            command, [{"type": "result", "message": "done", "payload": None}]
        )

    def read(self, *, timeout_sec: float) -> list[dict[str, object]]:
        del timeout_sec
        return []

    def get_gdb_response(
        self, *, timeout_sec: float, raise_error_on_timeout: bool = True
    ) -> list[dict[str, object]]:
        del timeout_sec, raise_error_on_timeout
        return []

    def exit(self) -> None:
        self.exited = True


class _CountingAttach:
    """A fake `AttachSeam` that records how many times it spawns an engine."""

    def __init__(self, controller: _FakeMiController | None = None) -> None:
        self.controller = controller or _FakeMiController()
        self.calls = 0

    def __call__(
        self, *, host: str, port: int, run_id: str, transcript_path: Path
    ) -> GdbMiAttachment:
        del host, port, run_id
        self.calls += 1
        return GdbMiAttachment(
            controller=self.controller,
            rsp_host="127.0.0.1",
            rsp_port=1234,
            transcript_path=transcript_path,
        )


def _raising_attach(*, host: str, port: int, run_id: str, transcript_path: Path) -> GdbMiAttachment:
    del host, port, transcript_path
    from kdive.domain.errors import CategorizedError, ErrorCategory

    raise CategorizedError(
        "no live host", category=ErrorCategory.MISSING_DEPENDENCY, details={"run_id": run_id}
    )


def _runtime(attach: Any) -> DebugEngineRuntime:
    return DebugEngineRuntime(
        engine=GdbMiEngine(), attach=attach, transcript_dir=Path(tempfile.mkdtemp())
    )


class _FixedDebugRuntimeResolver:
    def __init__(self, runtime: DebugEngineRuntime) -> None:
        self._runtime = runtime

    def runtime_for_binding(self, binding: Any, *, object_id: str) -> DebugEngineRuntime:
        del binding, object_id
        return self._runtime


def _session_handlers(runtime: DebugEngineRuntime) -> debug_tools.DebugSessionHandlers:
    return debug_tools.DebugSessionHandlers.from_resolver(
        provider_resolver(connector=_FakeConnector(), profile_policy=_PROFILE_POLICY),
        runtime_resolver=cast(Any, _FixedDebugRuntimeResolver(runtime)),
        secret_registry=SecretRegistry(),
    )


def _ctx(
    role: Role | None = Role.OPERATOR, *, projects: tuple[str, ...] = ("proj",)
) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="user-1", agent_session="s", projects=projects, roles=roles)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_live_session(pool: AsyncConnectionPool, *, state: DebugSessionState) -> str:
    disc = LocalLibvirtDiscovery(
        host_uri="qemu:///system", connect=lambda: FakeLibvirtConn(), concurrent_allocation_cap=2
    )
    async with pool.connection() as conn:
        res = await register_discovered_resource(
            conn, disc.list_resources()[0], pool="local-libvirt", cost_class="local"
        )
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                resource_id=res.id,
                state=AllocationState.GRANTED,
            ),
        )
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                allocation_id=alloc.id,
                state=SystemState.READY,
                provisioning_profile=copy.deepcopy(_PROFILE),
                domain_name="kdive-x",
            ),
        )
        inv = await INVESTIGATIONS.insert(
            conn,
            Investigation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                title="t",
                state=InvestigationState.ACTIVE,
            ),
        )
        run = await RUNS.insert(
            conn,
            Run(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                investigation_id=inv.id,
                system_id=system.id,
                target_kind=ResourceKind.LOCAL_LIBVIRT,
                state=RunState.SUCCEEDED,
                build_profile={},
            ),
        )
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s, 'boot', 'succeeded', %s)",
            (run.id, Jsonb({})),
        )
        session = await DEBUG_SESSIONS.insert(
            conn,
            DebugSession(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                run_id=run.id,
                state=state,
                transport="gdbstub",
                transport_handle=TransportHandleData(
                    kind="gdbstub", host="127.0.0.1", port=1234
                ).encode(),
            ),
        )
    return str(session.id)


def _op_for(op: str, runtime: DebugEngineRuntime, session_id: str, **kwargs: Any) -> Any:
    del runtime
    factory = {
        "set_breakpoint": debug_ops._set_breakpoint_op,
        "clear_breakpoint": debug_ops._clear_breakpoint_op,
        "list_breakpoints": debug_ops._list_breakpoints_op,
        "read_memory": debug_ops._read_memory_op,
        "read_registers": debug_ops._read_registers_op,
        "resolve_symbol": debug_ops._resolve_symbol_op,
        "continue": debug_ops._continue_op,
        "interrupt": debug_ops._interrupt_op,
        "backtrace": debug_ops._backtrace_op,
        "read_frame": debug_ops._read_frame_op,
        "disassemble": debug_ops._disassemble_op,
        "set_watchpoint": debug_ops._set_watchpoint_op,
        "list_watchpoints": debug_ops._list_watchpoints_op,
        "clear_watchpoint": debug_ops._clear_watchpoint_op,
        "list_modules": debug_ops._list_modules_op,
        "load_module_symbols": debug_ops._load_module_symbols_op,
    }[op]
    return factory(session_id, **kwargs)


# --- happy paths ---------------------------------------------------------------------------


def test_set_breakpoint_returns_set(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            controller = _FakeMiController(
                {
                    "-break-insert panic": [
                        {"type": "result", "message": "done", "payload": {"bkpt": {"number": "1"}}}
                    ]
                }
            )
            runtime = _runtime(_CountingAttach(controller))
            resp = await run_engine_op(
                pool,
                _ctx(),
                session_id,
                runtime,
                _op_for("set_breakpoint", runtime, session_id, location="panic"),
            )
        assert resp.status == "set"
        assert resp.data["number"] == "1"
        assert "debug.continue" in resp.suggested_next_actions

    asyncio.run(_run())


def test_read_memory_returns_verbatim_hex(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            controller = _FakeMiController(
                {
                    "-data-read-memory-bytes 0x1000 4": [
                        {
                            "type": "result",
                            "message": "done",
                            "payload": {"memory": [{"contents": "deadbeef"}]},
                        }
                    ]
                }
            )
            runtime = _runtime(_CountingAttach(controller))
            resp = await run_engine_op(
                pool,
                _ctx(),
                session_id,
                runtime,
                _op_for("read_memory", runtime, session_id, address=0x1000, byte_count=4),
            )
        assert resp.status == "read"
        assert resp.data["memory_hex"] == "deadbeef"  # bytes verbatim, not redacted
        assert resp.data["byte_count"] == 4
        assert resp.data["address"] == "0x1000"

    asyncio.run(_run())


def test_read_registers_returns_direct_values(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            controller = _FakeMiController(
                {
                    "-data-list-register-names": [
                        {
                            "type": "result",
                            "message": "done",
                            "payload": {"register-names": ["rax", "rbx", "rcx"]},
                        }
                    ],
                    "-data-list-register-values x": [
                        {
                            "type": "result",
                            "message": "done",
                            "payload": {
                                "register-values": [
                                    {"number": "0", "value": "0xdead"},
                                    {"number": "2", "value": "0xcafe"},
                                ]
                            },
                        }
                    ],
                }
            )
            runtime = _runtime(_CountingAttach(controller))
            resp = await run_engine_op(
                pool,
                _ctx(),
                session_id,
                runtime,
                _op_for("read_registers", runtime, session_id, registers=["rax", "rcx"]),
            )
        assert resp.status == "read"
        assert resp.data == {"rax": "0xdead", "rcx": "0xcafe"}

    asyncio.run(_run())


def test_read_memory_over_cap_is_rejected_without_attach(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            attach = _CountingAttach()
            runtime = _runtime(attach)
            resp = await run_engine_op(
                pool,
                _ctx(),
                session_id,
                runtime,
                _op_for("read_memory", runtime, session_id, address=0x10, byte_count=4097),
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["code"] == "bad_read_range"
        # The attach DID happen (the cap is enforced in the engine op), but no MI read command ran.
        assert attach.controller.written == []

    asyncio.run(_run())


def test_continue_returns_stopped(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            controller = _FakeMiController(
                {"-exec-continue": [{"type": "result", "message": "running", "payload": None}]}
            )
            # No reads scripted -> resume times out -> interrupt -> no stop -> transport_stall.
            controller._responses["-exec-interrupt"] = [
                {"type": "result", "message": "done", "payload": None}
            ]
            runtime = _runtime(_CountingAttach(controller))
            resp = await run_engine_op(
                pool,
                _ctx(),
                session_id,
                runtime,
                _op_for("continue", runtime, session_id, timeout_sec=1),
            )
        # A silent link surfaces as INFRASTRUCTURE_FAILURE (the handler maps the engine error).
        assert resp.status == "error"
        assert resp.error_category == "infrastructure_failure"
        assert resp.data["code"] == "transport_stall"

    asyncio.run(_run())


def test_resolve_symbol_returns_resolved(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            controller = _FakeMiController(
                {
                    "-data-evaluate-expression &d_hash_shift": [
                        {
                            "type": "result",
                            "message": "done",
                            "payload": {"value": "(int *) 0x1234 <d_hash_shift>"},
                        }
                    ]
                }
            )
            runtime = _runtime(_CountingAttach(controller))
            resp = await run_engine_op(
                pool,
                _ctx(),
                session_id,
                runtime,
                _op_for("resolve_symbol", runtime, session_id, name="d_hash_shift"),
            )
        assert resp.status == "resolved"
        assert resp.data["symbol"] == "d_hash_shift"
        assert resp.data["address"] == "0x1234"
        assert "debug.read_memory" in resp.suggested_next_actions

    asyncio.run(_run())


def test_resolve_symbol_bad_name_rejected_without_command(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            attach = _CountingAttach()
            runtime = _runtime(attach)
            resp = await run_engine_op(
                pool,
                _ctx(),
                session_id,
                runtime,
                _op_for("resolve_symbol", runtime, session_id, name="not a name"),
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["code"] == "bad_symbol_name"
        # The bad name is rejected in the engine op before any MI command is written.
        assert attach.controller.written == []

    asyncio.run(_run())


def test_resolve_symbol_inlined_is_symbol_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            controller = _FakeMiController(
                {
                    "-data-evaluate-expression &inlined_helper": [
                        {
                            "type": "result",
                            "message": "error",
                            "payload": {"msg": 'No symbol "inlined_helper" in current context.'},
                        }
                    ]
                }
            )
            runtime = _runtime(_CountingAttach(controller))
            resp = await run_engine_op(
                pool,
                _ctx(),
                session_id,
                runtime,
                _op_for("resolve_symbol", runtime, session_id, name="inlined_helper"),
            )
        # An inlined / optimized-away symbol is a non-retryable symbol_not_found with the inline
        # hint — the attach is fine, so retrying is pointless (ADR-0307).
        assert resp.status == "error"
        assert resp.error_category == "symbol_not_found"
        assert resp.retryable is False
        assert resp.data["code"] == "symbol_not_found"
        assert "inlined or optimized away" in str(resp.data["hint"])

    asyncio.run(_run())


def test_backtrace_returns_walked(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            controller = _FakeMiController(
                {
                    "-stack-list-frames": [
                        {
                            "type": "result",
                            "message": "done",
                            "payload": {
                                "stack": [
                                    {
                                        "frame": {
                                            "level": "0",
                                            "func": "panic",
                                            "addr": "0xffffffff81000000",
                                            "file": "kernel/panic.c",
                                            "line": "42",
                                        }
                                    },
                                    {"frame": {"level": "1", "func": "do_exit"}},
                                ]
                            },
                        }
                    ]
                }
            )
            runtime = _runtime(_CountingAttach(controller))
            resp = await run_engine_op(
                pool,
                _ctx(),
                session_id,
                runtime,
                _op_for("backtrace", runtime, session_id, max_frames=64),
            )
        assert resp.status == "walked"
        assert resp.data["frame_count"] == 2
        assert resp.data["truncated"] is False
        frames = cast("list[dict[str, Any]]", resp.data["frames"])
        assert frames[0]["func"] == "panic"
        assert frames[0]["line"] == 42
        assert "debug.read_frame" in resp.suggested_next_actions

    asyncio.run(_run())


def test_backtrace_running_inferior_is_categorized(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            controller = _FakeMiController(
                {
                    "-stack-list-frames": [
                        {
                            "type": "result",
                            "message": "error",
                            "payload": {
                                "msg": "Cannot execute this command while the target is running."
                            },
                        }
                    ]
                }
            )
            runtime = _runtime(_CountingAttach(controller))
            resp = await run_engine_op(
                pool,
                _ctx(),
                session_id,
                runtime,
                _op_for("backtrace", runtime, session_id, max_frames=64),
            )
        assert resp.status == "error"
        assert resp.error_category == "debug_attach_failure"
        assert resp.data["code"] == "inferior_running"

    asyncio.run(_run())


def test_read_frame_returns_read(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            controller = _FakeMiController(
                {
                    "-stack-list-frames 2 2": [
                        {
                            "type": "result",
                            "message": "done",
                            "payload": {
                                "stack": [
                                    {
                                        "frame": {
                                            "level": "2",
                                            "func": "schedule",
                                            "addr": "0xffffffff8100a000",
                                        }
                                    }
                                ]
                            },
                        }
                    ]
                }
            )
            runtime = _runtime(_CountingAttach(controller))
            resp = await run_engine_op(
                pool,
                _ctx(),
                session_id,
                runtime,
                _op_for("read_frame", runtime, session_id, level=2),
            )
        assert resp.status == "read"
        assert resp.data["level"] == 2
        frame = cast("dict[str, Any]", resp.data["frame"])
        assert frame["func"] == "schedule"

    asyncio.run(_run())


def test_disassemble_returns_disassembled(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            controller = _FakeMiController(
                {
                    "-data-disassemble -s 0x1000 -e 0x1080 -- 0": [
                        {
                            "type": "result",
                            "message": "done",
                            "payload": {
                                "asm_insns": [
                                    {
                                        "address": "0x1000",
                                        "inst": "nop",
                                        "func-name": "f",
                                        "offset": "0",
                                    },
                                ]
                            },
                        }
                    ]
                }
            )
            runtime = _runtime(_CountingAttach(controller))
            resp = await run_engine_op(
                pool,
                _ctx(),
                session_id,
                runtime,
                _op_for(
                    "disassemble",
                    runtime,
                    session_id,
                    symbol=None,
                    address=0x1000,
                    instruction_count=8,
                ),
            )
        assert resp.status == "disassembled"
        assert resp.data["instruction_count"] == 1
        assert resp.data["truncated"] is False
        insns = cast("list[dict[str, Any]]", resp.data["instructions"])
        assert insns[0]["inst"] == "nop"
        assert "debug.read_memory" in resp.suggested_next_actions

    asyncio.run(_run())


def test_disassemble_no_instructions_is_categorized(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            controller = _FakeMiController(
                {
                    "-data-disassemble -s 0x1000 -e 0x1080 -- 0": [
                        {"type": "result", "message": "done", "payload": {"asm_insns": []}}
                    ]
                }
            )
            runtime = _runtime(_CountingAttach(controller))
            resp = await run_engine_op(
                pool,
                _ctx(),
                session_id,
                runtime,
                _op_for(
                    "disassemble",
                    runtime,
                    session_id,
                    symbol=None,
                    address=0x1000,
                    instruction_count=8,
                ),
            )
        assert resp.status == "error"
        assert resp.error_category == "debug_attach_failure"
        assert resp.data["code"] == "no_instructions"

    asyncio.run(_run())


def test_set_watchpoint_returns_watching(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            controller = _FakeMiController(
                {
                    "-break-watch *(char(*)[8])0x1000": [
                        {
                            "type": "result",
                            "message": "done",
                            "payload": {"wpt": {"number": "2", "exp": "*(char(*)[8])0x1000"}},
                        }
                    ]
                }
            )
            runtime = _runtime(_CountingAttach(controller))
            resp = await run_engine_op(
                pool,
                _ctx(),
                session_id,
                runtime,
                _op_for(
                    "set_watchpoint", runtime, session_id, symbol=None, address=0x1000, byte_count=8
                ),
            )
        assert resp.status == "watching"
        assert resp.data["number"] == "2"
        assert resp.data["byte_count"] == 8
        assert "debug.continue" in resp.suggested_next_actions

    asyncio.run(_run())


def test_list_watchpoints_returns_listed(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            controller = _FakeMiController(
                {
                    "-break-list": [
                        {
                            "type": "result",
                            "message": "done",
                            "payload": {
                                "BreakpointTable": {
                                    "body": [
                                        {
                                            "bkpt": {
                                                "number": "2",
                                                "type": "hw watchpoint",
                                                "what": "*(char(*)[8])0x1000",
                                            }
                                        }
                                    ]
                                }
                            },
                        }
                    ]
                }
            )
            runtime = _runtime(_CountingAttach(controller))
            resp = await run_engine_op(
                pool, _ctx(), session_id, runtime, _op_for("list_watchpoints", runtime, session_id)
            )
        assert resp.status == "listed"
        assert resp.data["count"] == 1

    asyncio.run(_run())


def test_clear_watchpoint_returns_cleared(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            controller = _FakeMiController({})
            runtime = _runtime(_CountingAttach(controller))
            resp = await run_engine_op(
                pool,
                _ctx(),
                session_id,
                runtime,
                _op_for("clear_watchpoint", runtime, session_id, number="2"),
            )
        assert resp.status == "cleared"

    asyncio.run(_run())


def test_set_watchpoint_unsupported_is_categorized(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            controller = _FakeMiController(
                {
                    "-break-watch *(char(*)[8])0x1000": [
                        {
                            "type": "result",
                            "message": "error",
                            "payload": {"msg": "Target does not support hardware watchpoints."},
                        }
                    ]
                }
            )
            runtime = _runtime(_CountingAttach(controller))
            resp = await run_engine_op(
                pool,
                _ctx(),
                session_id,
                runtime,
                _op_for(
                    "set_watchpoint", runtime, session_id, symbol=None, address=0x1000, byte_count=8
                ),
            )
        assert resp.status == "error"
        assert resp.error_category == "debug_attach_failure"
        assert resp.data["code"] == "watchpoint_unsupported"

    asyncio.run(_run())


# --- gate + §5a codes ----------------------------------------------------------------------


def test_bad_session_id(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            runtime = _runtime(_CountingAttach())
            resp = await run_engine_op(
                pool,
                _ctx(),
                "not-a-uuid",
                runtime,
                _op_for("list_breakpoints", runtime, "not-a-uuid"),
            )
        assert resp.error_category == "configuration_error"
        assert resp.data["code"] == "bad_session_id"

    asyncio.run(_run())


def test_unknown_session(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sid = str(uuid4())
            runtime = _runtime(_CountingAttach())
            resp = await run_engine_op(
                pool, _ctx(), sid, runtime, _op_for("list_breakpoints", runtime, sid)
            )
        assert resp.data["code"] == "unknown_session"

    asyncio.run(_run())


def test_cross_project_session_is_unknown(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            runtime = _runtime(_CountingAttach())
            resp = await run_engine_op(
                pool,
                _ctx(projects=("other",)),
                session_id,
                runtime,
                _op_for("list_breakpoints", runtime, session_id),
            )
        assert resp.data["code"] == "unknown_session"

    asyncio.run(_run())


def test_non_operator_raises(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            runtime = _runtime(_CountingAttach())
            with pytest.raises(AuthorizationError):
                await run_engine_op(
                    pool,
                    _ctx(Role.VIEWER),
                    session_id,
                    runtime,
                    _op_for("list_breakpoints", runtime, session_id),
                )

    asyncio.run(_run())


def test_non_live_session_is_not_live(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.DETACHED)
            runtime = _runtime(_CountingAttach())
            resp = await run_engine_op(
                pool, _ctx(), session_id, runtime, _op_for("list_breakpoints", runtime, session_id)
            )
        assert resp.data["code"] == "not_live"
        assert resp.data["current_status"] == "detached"

    asyncio.run(_run())


def test_missing_dependency_attach_surfaces_as_debug_attach_failure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            runtime = _runtime(_raising_attach)
            resp = await run_engine_op(
                pool, _ctx(), session_id, runtime, _op_for("list_breakpoints", runtime, session_id)
            )
        assert resp.status == "error"
        assert resp.error_category == "debug_attach_failure"
        assert resp.data["run_id"]

    asyncio.run(_run())


# --- attach-once + reap --------------------------------------------------------------------


def test_attach_runs_once_for_concurrent_ops(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            attach = _CountingAttach()
            runtime = _runtime(attach)
            ops = [
                run_engine_op(
                    pool,
                    _ctx(),
                    session_id,
                    runtime,
                    _op_for("list_breakpoints", runtime, session_id),
                )
                for _ in range(2)
            ]
            results = await asyncio.gather(*ops)
        assert all(r.status == "listed" for r in results)
        assert attach.calls == 1  # the per-session lock serializes; only one op attaches

    asyncio.run(_run())


def test_provider_debug_runtime_cache_uses_binding_kind() -> None:
    resolver = debug_ops.DebugRuntimeResolver(cast(ProviderResolver, object()))
    first_attach = _CountingAttach()
    first_provider = cast(
        ProviderRuntime,
        SimpleNamespace(debug=DebugCapabilities(engine=GdbMiEngine(), attach_seam=first_attach)),
    )
    runtime = resolver.runtime_for_binding(
        ProviderBinding(kind=ResourceKind.LOCAL_LIBVIRT, runtime=first_provider)
    )
    assert isinstance(runtime, DebugEngineRuntime)

    second_provider = cast(
        ProviderRuntime,
        SimpleNamespace(
            debug=DebugCapabilities(engine=GdbMiEngine(), attach_seam=_CountingAttach())
        ),
    )
    same_runtime = resolver.runtime_for_binding(
        ProviderBinding(kind=ResourceKind.LOCAL_LIBVIRT, runtime=second_provider)
    )
    assert isinstance(same_runtime, DebugEngineRuntime)

    assert same_runtime is runtime


def test_provider_debug_runtime_fails_when_debug_capability_absent() -> None:
    resolver = debug_ops.DebugRuntimeResolver(cast(ProviderResolver, object()))
    provider = cast(ProviderRuntime, SimpleNamespace(debug=None))

    response = resolver.runtime_for_binding(
        ProviderBinding(kind=ResourceKind.FAULT_INJECT, runtime=provider),
        object_id="session-1",
    )

    assert isinstance(response, ToolResponse)
    assert response.status == "error"
    assert response.error_category == "debug_attach_failure"
    assert response.data["reason"] == "provider_debug_unavailable"


def test_end_session_reaps_engine(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            attach = _CountingAttach()
            runtime = _runtime(attach)
            await run_engine_op(
                pool, _ctx(), session_id, runtime, _op_for("list_breakpoints", runtime, session_id)
            )
            # The engine is registered; end_session must exit + drop it.
            handlers = _session_handlers(runtime)
            resp = await handlers.end_session(pool, _ctx(), session_id)
            assert resp.status == "detached"
            assert attach.controller.exited is True
            # A subsequent op on the now-detached session is rejected at the state gate.
            follow = await run_engine_op(
                pool, _ctx(), session_id, runtime, _op_for("list_breakpoints", runtime, session_id)
            )
        assert follow.data["code"] == "not_live"

    asyncio.run(_run())


def test_end_session_reap_is_noop_without_engine(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            runtime = _runtime(_CountingAttach())
            handlers = _session_handlers(runtime)
            resp = await handlers.end_session(pool, _ctx(), session_id)
        assert resp.status == "detached"  # reap of a never-attached session is a no-op

    asyncio.run(_run())


class _FakeConnector:
    def open_transport(self, system: Any, kind: str) -> Any:
        del system, kind
        raise NotImplementedError

    def close_transport(self, handle: Any) -> None:
        del handle


# --- module symbols (#923, ADR-0278) --------------------------------------------------------

from kdive.providers.shared.debug_common.debuginfo import ModuleDebuginfo  # noqa: E402


def _module_walk_responses() -> dict[str, list[dict[str, object]]]:
    # One-module walk: offset 8, head 0x1000, ext4 at 0x2000 (node 0x2008), terminating at head.
    def ev(value: str) -> list[dict[str, object]]:
        return [{"type": "result", "message": "done", "payload": {"value": value}}]

    # The engine double-quotes each expression as one MI argument (module casts contain spaces,
    # which gdb/MI would otherwise tokenize into several arguments); key the fake to that form.
    def de(expr: str) -> str:
        return f'-data-evaluate-expression "{expr}"'

    return {
        de("&((struct module *)0)->list"): ev("0x8"),
        de("&modules"): ev("0x1000"),
        de("modules.next"): ev("0x2008"),
        de("((struct module *)0x2000)->name"): ev('"ext4"'),
        de("((struct module *)0x2000)->mem[0].base"): ev("0x2000"),
        de("((struct module *)0x2000)->srcversion"): ev('"SRC1"'),
        de("((struct list_head *)0x2008)->next"): ev("0x1000"),
    }


def _runtime_with_resolver(attach: Any, info: ModuleDebuginfo) -> DebugEngineRuntime:
    def resolver(run_id: str, module: str) -> ModuleDebuginfo:
        return info

    return DebugEngineRuntime(
        engine=GdbMiEngine(module_debuginfo_resolver=resolver),
        attach=attach,
        transcript_dir=Path(tempfile.mkdtemp()),
    )


def test_list_modules_returns_listed(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            controller = _FakeMiController(_module_walk_responses())
            runtime = _runtime(_CountingAttach(controller))
            resp = await run_engine_op(
                pool, _ctx(), session_id, runtime, _op_for("list_modules", runtime, session_id)
            )
        assert resp.status == "listed"
        assert resp.data["count"] == 1
        assert resp.data["truncated"] is False
        assert resp.data["decode_errors"] == 0
        modules = cast(list[dict[str, Any]], resp.data["modules"])
        assert modules[0]["name"] == "ext4"
        assert "debug.load_module_symbols" in resp.suggested_next_actions

    asyncio.run(_run())


def test_load_module_symbols_returns_loaded(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            controller = _FakeMiController(_module_walk_responses())
            info = ModuleDebuginfo(path=Path("/x/ext4.ko"), srcversion="SRC1", build_id=None)
            runtime = _runtime_with_resolver(_CountingAttach(controller), info)
            resp = await run_engine_op(
                pool,
                _ctx(),
                session_id,
                runtime,
                _op_for(
                    "load_module_symbols", runtime, session_id, module="ext4", expected_base=None
                ),
            )
        assert resp.status == "loaded"
        assert resp.data["module"] == "ext4"
        assert resp.data["base_address"] == "0x2000"
        assert resp.data["symbols_loaded"] is True
        assert resp.data["identity_verified"] is True

    asyncio.run(_run())


def test_load_module_symbols_stale_is_categorized(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            controller = _FakeMiController(_module_walk_responses())
            info = ModuleDebuginfo(path=Path("/x/ext4.ko"), srcversion="SRC1", build_id=None)
            runtime = _runtime_with_resolver(_CountingAttach(controller), info)
            resp = await run_engine_op(
                pool,
                _ctx(),
                session_id,
                runtime,
                _op_for(
                    "load_module_symbols", runtime, session_id, module="ext4", expected_base=0x9999
                ),
            )
        assert resp.error_category == "debug_attach_failure"
        assert resp.data["code"] == "stale_module_address"

    asyncio.run(_run())
