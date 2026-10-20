"""Local GDB debug tool cells over HTTP (#2813, ADR-0722).

A functional cell provisions the lane image with a gdbstub through
:func:`~tests.integration.live_stack.tool_cells.on_lane_system`, boots the verified ``longterm``
kernel fixture (:func:`~tests.integration.live_stack.capture_cells.boot`), which uploads the Run's
``vmlinux`` and loads ``loop``, then reads its ground truth from the guest before any attach halts
it: ``/proc/version``, ``/proc/kallsyms`` and the ``loop`` ``.text`` base. The tool under test goes
through the cell's exposure with its gate role; the session, breakpoint and resume calls around
it use the frame's operator client. A ``vfs_read`` stop is the controlled workload: an SSH client
banner written while the guest resumes makes guest ``sshd`` call ``read(2)``.

A rejection cell aims at the stack's :func:`debug_target`: a torn-down fixture System, its Run and
an ended gdbstub session. ``debug.list_sessions`` filters a denied caller's view, so its
authorization and project-isolation cells prove denied visibility against a viewer who sees the
target session (ADR-0756, amended 2026-10-14). No rejection record carries a kernel field, since
no rejection cell declares kernel inputs.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import re
import secrets
import shutil
import subprocess
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import psycopg

from kdive.domain.errors import ErrorCategory
from kdive.mcp.dev_harness import LiveStackClient, OidcIssuer
from kdive.mcp.responses import ToolResponse
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack.capture_cells import (
    boot,
    lane_keys,
    protected_state,
    require_tool,
    section,
)
from tests.integration.live_stack.image_smoke import Endpoint, ssh
from tests.integration.live_stack.power_snapshots import grants
from tests.integration.live_stack.scenario import (
    CellRun,
    ScenarioStop,
    provision_catalog,
)
from tests.integration.live_stack.spine import ok, scalar
from tests.integration.live_stack.tool_cells import (
    Boundary,
    FilteredListControl,
    Grants,
    Guest,
    HttpCaller,
    Rejection,
    Snapshot,
    on_lane_system,
    one,
    settled,
)

TOOLS = (
    "debug.advance",
    "debug.backtrace",
    "debug.clear_breakpoint",
    "debug.clear_watchpoint",
    "debug.continue",
    "debug.disassemble",
    "debug.end_session",
    "debug.get_session",
    "debug.interrupt",
    "debug.list_breakpoints",
    "debug.list_modules",
    "debug.list_sessions",
    "debug.list_watchpoints",
    "debug.load_module_symbols",
    "debug.read_frame",
    "debug.read_memory",
    "debug.read_registers",
    "debug.resolve_symbol",
    "debug.set_breakpoint",
    "debug.set_watchpoint",
    "debug.start_session",
)
# Each tool's project gate (src/kdive/mcp/exposure.py).
GATES = {tool: "contributor" for tool in TOOLS} | {
    "debug.get_session": "viewer",
    "debug.list_sessions": "viewer",
}
_BELOW: dict[str, str | None] = {"contributor": "viewer", "viewer": None}
# The fixed valid arguments beside each tool's id, for the rejection calls.
_EXTRA: dict[str, dict[str, object]] = {
    "debug.advance": {"mode": "out"},
    "debug.clear_breakpoint": {"number": "1"},
    "debug.clear_watchpoint": {"number": "1"},
    "debug.continue": {"timeout_sec": 1.0},
    "debug.disassemble": {"symbol": "vfs_read", "instruction_count": 4},
    "debug.load_module_symbols": {"module": "loop"},
    "debug.read_frame": {"level": 0},
    "debug.read_memory": {"address": 4096, "byte_count": 8},
    "debug.read_registers": {"registers": ["rip"]},
    "debug.resolve_symbol": {"name": "vfs_read"},
    "debug.set_breakpoint": {"location": "vfs_read"},
    "debug.set_watchpoint": {"symbol": "jiffies_64", "byte_count": 8},
}
_LIST = "debug.list_sessions"
# The lowest x86_64 kernel-half address: a register or stack pointer in the kernel is above it.
KERNEL_BASE = 0xFFFF800000000000
BREAK_AT = "vfs_read"
# vfs_read's parameters in the fixture's v6.18 fs/read_write.c, and the x86_64 SysV registers
# holding them at its entry, where gdb places ``break vfs_read`` in the fixture (ADR-0781).
_VFS_READ_ARGS = ("file", "buf", "count", "pos")
_ARG_REGISTERS = ("rdi", "rsi", "rdx", "rcx")
WATCH = "jiffies_64"
BANNER = "linux_banner"
# A [loop] text function in .text, which load_module_symbols relocates.
MODULE, MODULE_SYMBOL = "loop", "loop_queue_rq"
# The read(2) family's paths into vfs_read; any of them may take the breakpoint (spec, failure
# model): the banner's read, or an ambient read or pread64.
_READ_CALLERS = frozenset({"ksys_read", "__x64_sys_read", "ksys_pread64", "__x64_sys_pread64"})
# The engine's missing-debugger details (gdbmi/core/engine.py, native and cross-arch).
_MISSING_GDB = ("missing required gdb tool", "missing a multiarch-capable gdb")
_STOP_WAIT_S = 30.0
_NO_STOP_WAIT_S = 5.0
_ANSWER_S = 5.0
_RESUME_LEAD_S = 1.0
_REANSWER_S = 60.0


# --- ground truth -------------------------------------------------------------------------


@dataclass(frozen=True)
class Truth:
    """What the running guest says before any attach: the debugger's answers are checked here.

    ``symbols`` holds the first core address of each name in ``/proc/kallsyms``, ``module`` the
    ``[loop]`` text symbols, ``text`` every text address in order, and ``loop_text`` the
    ``loop`` module's ``.text`` base from ``/sys/module``.
    """

    version: bytes
    symbols: Mapping[str, int]
    module: Mapping[str, int]
    text: tuple[int, ...]
    loop_text: int

    def at(self, name: str) -> int:
        address = self.symbols.get(name)
        assert address, f"/proc/kallsyms names no {name}"
        return address

    def end_of(self, name: str) -> int:
        """The next text address above ``name``'s: where its function ends at the latest."""
        start = self.at(name)
        following = [a for a in self.text if a > start]
        assert following, f"no text symbol follows {name}"
        return following[0]

    def within(self, name: str, address: int) -> bool:
        return self.at(name) <= address < self.end_of(name)


def parse_kallsyms(
    text: str, module: str = MODULE
) -> tuple[dict[str, int], dict[str, int], tuple[int, ...]]:
    """Core symbols, ``module``'s text symbols, and sorted text addresses of ``/proc/kallsyms``.

    A pointer-restricted listing, all zero addresses, fails: no comparison could pass on it.
    """
    core: dict[str, int] = {}
    owned: dict[str, int] = {}
    addresses: set[int] = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        address, kind, name = int(parts[0], 16), parts[1], parts[2]
        owner = parts[3] if len(parts) > 3 else None
        if kind in ("t", "T"):
            addresses.add(address)
        if owner is None:
            core.setdefault(name, address)
        elif owner == f"[{module}]" and kind in ("t", "T"):
            owned.setdefault(name, address)
    assert any(addresses), "/proc/kallsyms lists no nonzero text address (pointer restriction)"
    return core, owned, tuple(sorted(addresses))


def system_map_address(system_map: str, name: str) -> int:
    """``name``'s address in a fixture ``System.map``."""
    for line in system_map.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == name:
            return int(parts[0], 16)
    raise AssertionError(f"System.map names no {name}")


def hex_value(value: object) -> int:
    """A gdb hex value such as ``0xffffffff81234567`` or ``0xffffffff81234567 <vfs_read+4>``."""
    assert isinstance(value, str) and value.startswith("0x"), f"not a hex value: {value!r}"
    return int(value.split()[0], 16)


@dataclass(frozen=True)
class Instruction:
    """One instruction of the fixture ``vmlinux`` as ``objdump`` decodes it."""

    address: int
    text: str
    target: int | None

    @property
    def mnemonic(self) -> str:
        return self.text.split()[0]

    def transfers(self) -> bool:
        """A jump, call or return: the next instruction is not necessarily the one after it."""
        return any(w.startswith(("j", "call", "ret")) for w in self.text.split()[:2])


_OBJDUMP_LINE = re.compile(r"^\s*([0-9a-f]+):\s+(\S.*?)\s*$")
_TARGET = re.compile(r"\b([0-9a-f]{8,}) <")


def parse_objdump(text: str) -> list[Instruction]:
    """The instructions of ``objdump -d --no-show-raw-insn`` output, in address order."""
    listed = []
    for line in text.splitlines():
        found = _OBJDUMP_LINE.match(line)
        if found is None or found.group(2).startswith("("):
            continue
        target = _TARGET.search(found.group(2))
        listed.append(
            Instruction(
                int(found.group(1), 16),
                found.group(2),
                int(target.group(1), 16) if target else None,
            )
        )
    return listed


async def fixture_code(vmlinux: Path, start: int, stop: int) -> list[Instruction]:
    """The fixture binary's instructions in ``[start, stop)``: ground truth beside the debugger.

    ``blocked`` without ``objdump`` on the test host.
    """
    tool = shutil.which("objdump")
    if tool is None:
        raise ScenarioStop(Outcome.BLOCKED, "objdump (binutils) is not on the test host's PATH")
    argv = [tool, "-d", "--no-show-raw-insn", f"--start-address={start:#x}"]
    argv += [f"--stop-address={stop:#x}", str(vmlinux)]

    def decode() -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed argv  # nosec B603
            argv, capture_output=True, text=True, timeout=120.0, check=False
        )

    result = await asyncio.to_thread(decode)
    assert result.returncode == 0, f"objdump exited {result.returncode}"
    listed = parse_objdump(result.stdout)
    assert listed and listed[0].address == start, "objdump decoded nothing at the symbol"
    return listed


def next_pcs(code: list[Instruction], pc: int) -> set[int]:
    """Where one instruction step from ``pc`` may land: the next instruction, or its target."""
    index = next((i for i, ins in enumerate(code) if ins.address == pc), None)
    assert index is not None, f"{pc:#x} is not an instruction boundary of the fixture binary"
    assert index + 1 < len(code), "the stop is on the function's last instruction"
    here, following = code[index], code[index + 1].address
    if not here.transfers():
        return {following}
    assert not here.mnemonic.startswith("ret") and here.target is not None, (
        f"cannot predict the step from {here.mnemonic} at {pc:#x}"
    )
    return {following, here.target}


async def return_sites(vmlinux: Path, truth: Truth, callee: str) -> set[int]:
    """The addresses after each ``call <callee>`` in the read(2)-family callers of the binary."""
    target = truth.at(callee)
    sites: set[int] = set()
    for caller in sorted(_READ_CALLERS & set(truth.symbols)):
        code = await fixture_code(vmlinux, truth.at(caller), truth.end_of(caller))
        sites |= {
            after.address
            for ins, after in zip(code, code[1:], strict=False)
            if ins.mnemonic.startswith("call") and ins.target == target
        }
    assert sites, f"no read(2)-family caller of the fixture calls {callee}"
    return sites


async def _guest(endpoint: Endpoint, key: Path, command: str) -> str:
    result = await asyncio.to_thread(ssh, endpoint, key, command)
    assert result.returncode == 0, f"guest command exited {result.returncode}"
    return result.stdout


async def read_truth(endpoint: Endpoint, key: Path, tree: Path) -> Truth:
    """The guest's facts, with ``vfs_read`` matching the fixture's unrandomised ``System.map``."""
    version = (await _guest(endpoint, key, "cat /proc/version")).encode()
    core, owned, text = parse_kallsyms(await _guest(endpoint, key, "cat /proc/kallsyms"))
    base = (await _guest(endpoint, key, f"cat /sys/module/{MODULE}/sections/.text")).strip()
    truth = Truth(version, core, owned, text, hex_value(base))
    mapped = system_map_address((tree / "System.map").read_text(encoding="utf-8"), BREAK_AT)
    assert truth.at(BREAK_AT) == mapped, "the guest's vfs_read is not the fixture's (KASLR?)"
    return truth


async def answers(endpoint: Endpoint, timeout: float = _ANSWER_S) -> bool:
    """Whether guest ``sshd`` sends its banner: false while the debugger halts the guest."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(endpoint.host, endpoint.port), timeout
        )
    except TimeoutError, OSError:
        return False
    try:
        line = await asyncio.wait_for(reader.readline(), timeout)
    except TimeoutError:
        return False
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(writer.wait_closed(), timeout)
    return line.startswith(b"SSH-")


async def answers_again(endpoint: Endpoint) -> bool:
    """Whether the guest answers within ``_REANSWER_S`` after a detach resumed it."""
    deadline = asyncio.get_running_loop().time() + _REANSWER_S
    while asyncio.get_running_loop().time() < deadline:
        if await answers(endpoint):
            return True
    return False


# --- provisioning ---------------------------------------------------------------------------


def gdbstub_profile(profile: Mapping[str, object]) -> dict[str, object]:
    """``profile`` with the local-libvirt gdbstub on and nothing else; the input is unchanged.

    No ``crashkernel``: that would resolve the capture method to kdump and drop ``nokaslr``.
    """
    edited = copy.deepcopy(dict(profile))
    local = cast(dict[str, Any], edited["provider"])["local-libvirt"]
    local["debug"] = {**local.get("debug", {}), "gdbstub": True}
    return edited


async def provision_gdbstub(
    op: LiveStackClient, allocation: str, profile: dict[str, object]
) -> str:
    return await provision_catalog(op, allocation, gdbstub_profile(profile))


def checked(env: ToolResponse, phase: str) -> ToolResponse:
    """``env`` when it succeeded; ``blocked`` when the stack lacks gdb or another dependency."""
    # The engine's missing-gdb error reaches the envelope as its detail alone: the op mapping
    # turns it into debug_attach_failure and the serializer drops its missing_tools list.
    if env.error_category == ErrorCategory.DEBUG_ATTACH_FAILURE.value and (
        env.detail or ""
    ).startswith(_MISSING_GDB):
        raise ScenarioStop(Outcome.BLOCKED, f"{phase}: the server host lacks a debugger")
    return require_tool(env, phase)


# --- functional cells -----------------------------------------------------------------------


@dataclass
class Session:
    """A functional cell's booted fixture System, its ground truth and who issues each call."""

    run: CellRun
    guest: Guest
    caller: HttpCaller
    grants: Grants
    manifest: Mapping[str, Any]
    run_id: str
    endpoint: Endpoint
    truth: Truth
    db_url: str
    tree: Path
    sessions: list[str] = field(default_factory=list)

    @property
    def variant(self) -> str:
        return self.run.cell.scenario_id.split("/")[2]

    async def exposed(self, **args: object) -> ToolResponse:
        """The tool under test, through the cell's exposure and token."""
        tool = self.run.cell.operation
        # Minted per call: a functional sequence can outlast a token's lifetime.
        token = self.caller.token(self.grants)
        result = await self.caller.call(tool, args, token, discover=True)
        return checked(result, tool) if isinstance(result, ToolResponse) else one(result)

    async def raw(self, tool: str, /, **args: object) -> ToolResponse:
        """A supporting call through the frame's operator client, whatever it answers."""
        env = await self.guest.op.call_tool(tool, **args)
        assert isinstance(env, ToolResponse), f"{tool} returned a list, expected one envelope"
        return env

    async def op(self, tool: str, /, **args: object) -> ToolResponse:
        """A supporting call through the frame's operator client, which must succeed."""
        return checked(await self.raw(tool, **args), tool)

    async def start(self, transport: str = "gdbstub") -> str:
        env = await self.op("debug.start_session", run_id=self.run_id, transport=transport)
        self.sessions.append(env.object_id)
        return env.object_id

    async def end(self, session_id: str) -> None:
        env = await self.op("debug.end_session", session_id=session_id)
        assert env.status == "detached", f"the session ended {env.status}"
        self.sessions.remove(session_id)

    async def end_all(self) -> None:
        """End every session this cell started and still holds."""
        for session_id in list(self.sessions):
            await self.end(session_id)

    async def register(self, session_id: str, name: str = "rip") -> int:
        env = await self.op("debug.read_registers", session_id=session_id, registers=[name])
        return hex_value(env.data.get(name))

    async def frames(self, session_id: str) -> list[dict[str, Any]]:
        env = await self.op("debug.backtrace", session_id=session_id)
        listed = env.data.get("frames")
        assert isinstance(listed, list) and listed, "the backtrace has no frames"
        return [cast(dict[str, Any], f) for f in listed]

    async def breakpoint(self, session_id: str, location: str = BREAK_AT) -> str:
        env = await self.op("debug.set_breakpoint", session_id=session_id, location=location)
        return str(env.data["number"])

    async def breakpoints(self, session_id: str) -> int:
        env = await self.op("debug.list_breakpoints", session_id=session_id)
        return cast(int, env.data["count"])

    async def watchpoint(self, session_id: str) -> str:
        env = await self.op(
            "debug.set_watchpoint", session_id=session_id, symbol=WATCH, byte_count=8
        )
        return str(env.data["number"])

    async def word(self, session_id: str, name: str) -> int:
        address = self.truth.at(name)
        env = await self.op(
            "debug.read_memory", session_id=session_id, address=address, byte_count=8
        )
        return int.from_bytes(bytes.fromhex(str(env.data["memory_hex"])), "little")

    async def resumed(self, call: Awaitable[ToolResponse]) -> ToolResponse:
        """Await a resume while a client banner makes guest ``sshd`` call ``read(2)``."""
        task = asyncio.ensure_future(call)
        writer: asyncio.StreamWriter | None = None
        try:
            await asyncio.sleep(_RESUME_LEAD_S)
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.endpoint.host, self.endpoint.port), _STOP_WAIT_S
            )
            writer.write(b"SSH-2.0-kdive-debug-cell\r\n")
            await writer.drain()
            return await task
        finally:
            if not task.done():
                task.cancel()
            if writer is not None:
                writer.close()

    async def run_free(self, session_id: str) -> None:
        """Resume without waiting; the guest must be left running."""
        env = await self.op("debug.continue", session_id=session_id, wait=False)
        assert env.status == "running", f"the non-waiting continue answered {env.status}"

    async def proceed(self, session_id: str, wait: float = _STOP_WAIT_S) -> ToolResponse:
        """An operator ``debug.continue`` with the read workload."""
        return await self.resumed(
            self.op("debug.continue", session_id=session_id, timeout_sec=wait)
        )

    async def stop_in_vfs_read(self, session_id: str) -> int:
        """Run to a ``vfs_read`` hit, clear the breakpoint, and return the stopped ``rip``."""
        number = await self.breakpoint(session_id)
        stop = await self.proceed(session_id)
        require_stop(stop, "breakpoint-hit")
        await self.op("debug.clear_breakpoint", session_id=session_id, number=number)
        rip = await self.register(session_id)
        assert self.truth.within(BREAK_AT, rip), "the breakpoint stop is not in vfs_read"
        return rip


def require_stop(env: ToolResponse, reason: str) -> None:
    """A ``stopped`` envelope for ``reason`` that did not time out."""
    assert env.status == "stopped", f"expected a stop, got {env.status}"
    assert env.data.get("timed_out") is False, "the resume timed out"
    assert env.data.get("reason") == reason, f"stopped for {env.data.get('reason')!r}"


def require_no_stop(env: ToolResponse) -> None:
    """A resume that waited out its timeout: nothing it should no longer have stopped it."""
    assert env.status == "stopped" and env.data.get("timed_out") is True, (
        f"the resume stopped for {env.data.get('reason')!r} before its timeout"
    )


async def start_body(s: Session) -> dict[str, object]:
    """A started session is live, usable, and holds the System's only attach of its transport."""
    transport = s.variant
    env = await s.exposed(run_id=s.run_id, transport=transport)
    s.sessions.append(env.object_id)
    assert env.status == "live", f"the session started {env.status}"
    read = await s.op("debug.get_session", session_id=env.object_id)
    record = (read.status, read.data.get("transport"), read.data.get("run_id"))
    assert record == ("live", transport, s.run_id), f"the session record reads {record}"
    assert read.data.get("system_id") == s.guest.system_id, "the session names another System"
    if transport == "gdbstub":
        usable = {"registers_read": await s.register(env.object_id) > 0}
    else:
        assert "missing_debuginfo" not in env.data, "drgn-live attached without debuginfo"
        report = await s.op("introspect.run", session_id=env.object_id, helper="sysinfo")
        release = section(report.data.get("report"), "sysinfo")["release"]
        usable = {"release_matches": release == s.manifest["release"]}
    assert all(usable.values()), f"the {transport} session is not usable: {usable}"
    second = await s.raw("debug.start_session", run_id=s.run_id, transport=transport)
    if second.error_category is None and second.object_id:
        s.sessions.append(second.object_id)  # an admitted second attach still gets ended
    assert second.error_category == ErrorCategory.TRANSPORT_CONFLICT.value, (
        f"a second {transport} attach answered {second.error_category}"
    )
    await s.end(env.object_id)
    return {"transport": transport, "record": "live", **usable, "second_attach": "conflict"}


async def end_body(s: Session) -> dict[str, object]:
    """Ending detaches, resumes the halted guest and releases the single attach."""
    session_id = await s.start()
    await s.register(session_id)
    assert not await answers(s.endpoint), "the guest answers while attached"
    env = await s.exposed(session_id=session_id)
    s.sessions.remove(session_id)
    assert env.status == "detached", f"the session ended {env.status}"
    read = await s.op("debug.get_session", session_id=session_id)
    assert read.status == "detached", f"the session record reads {read.status}"
    assert await answers_again(s.endpoint), "the guest did not resume after the detach"
    again = await s.start()
    await s.register(again)
    await s.end(again)
    return {"record": "detached", "guest_resumed": True, "reattach": True}


async def get_body(s: Session) -> dict[str, object]:
    """The read session matches the one started."""
    session_id = await s.start()
    env = await s.exposed(session_id=session_id)
    seen = {k: env.data.get(k) for k in ("project", "run_id", "system_id", "transport")}
    expected = {
        "project": s.guest.project,
        "run_id": s.run_id,
        "system_id": s.guest.system_id,
        "transport": "gdbstub",
    }
    assert (env.object_id, env.status, seen) == (session_id, "live", expected), "record differs"
    await s.end(session_id)
    return {"status": "live", "fields_match": sorted(expected)}


async def _records(db_url: str, run_id: str) -> list[tuple[str, str]]:
    """The Run's session rows, newest first, read directly from the database."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.set_read_only(True)
        cursor = await conn.execute(
            "SELECT id::text, state FROM debug_sessions WHERE run_id = %s "
            "ORDER BY created_at DESC, id DESC",
            (run_id,),
        )
        return [(str(row[0]), str(row[1])) for row in await cursor.fetchall()]


async def list_body(s: Session) -> dict[str, object]:
    """Filters and newest-first order match the session rows."""
    first = await s.start()
    await s.end(first)
    second = await s.start()
    rows = await _records(s.db_url, s.run_id)
    assert rows == [(second, "live"), (first, "detached")], f"session rows read {rows}"
    pages = {}
    for state in (None, "live", "detached"):
        request: dict[str, object] = {"run_id": s.run_id}
        if state is not None:
            request["state"] = state
        env = await s.exposed(request=request)
        pages[state or "all"] = [(i.object_id, i.status) for i in env.items]
    assert pages == {
        "all": rows,
        "live": [r for r in rows if r[1] == "live"],
        "detached": [r for r in rows if r[1] == "detached"],
    }, f"the listed sessions differ from the rows: {pages}"
    await s.end(second)
    return {"filters": ["run_id", "state"], "order": "newest-first", "rows": len(rows)}


async def continue_body(s: Session) -> dict[str, object]:
    """Resuming reaches the ``vfs_read`` breakpoint with ``rip`` in ``vfs_read``."""
    session_id = await s.start()
    number = await s.breakpoint(session_id)
    stop = await s.resumed(s.exposed(session_id=session_id, timeout_sec=_STOP_WAIT_S))
    require_stop(stop, "breakpoint-hit")
    rip = await s.register(session_id)
    assert s.truth.within(BREAK_AT, rip), "the stop is not in vfs_read"
    await s.op("debug.clear_breakpoint", session_id=session_id, number=number)
    await s.end(session_id)
    return {"reason": "breakpoint-hit", "rip_in": BREAK_AT}


async def interrupt_body(s: Session) -> dict[str, object]:
    """The interrupt halts a guest left running between calls and says what halted it (#3244).

    ``debug.continue`` with ``wait=false`` leaves the guest running, so it answers SSH while
    attached. The breakpoint arm records whichever path the ``vfs_read`` stop takes: the
    non-waiting resume's own reply, or a waiting continue that collects it without resuming
    again. Ending a session while its guest runs must leave the guest answering.
    """
    session_id = await s.start()
    await s.run_free(session_id)
    assert await answers(s.endpoint), "the guest does not run between calls"
    env = await s.exposed(session_id=session_id)
    assert env.status == "stopped" and env.data.get("timed_out") is False, "no clean stop"
    halted_by, reason = env.data.get("halted_by"), env.data.get("reason")
    assert (halted_by, reason) == ("interrupt", "signal-received"), (
        f"the interrupt reported {halted_by!r} for {reason!r}"
    )
    assert not await answers(s.endpoint), "the guest still answers after the interrupt"
    assert await s.register(session_id) >= KERNEL_BASE, "rip is not a kernel address"
    number = await s.breakpoint(session_id)
    started = await s.op("debug.continue", session_id=session_id, wait=False)
    path = "resume_reply" if started.status == "stopped" else "waiting_continue"
    if path == "waiting_continue":
        assert started.status == "running", f"the resume answered {started.status}"
        started = await s.resumed(
            s.op("debug.continue", session_id=session_id, timeout_sec=_STOP_WAIT_S)
        )
    require_stop(started, "breakpoint-hit")
    await s.op("debug.clear_breakpoint", session_id=session_id, number=number)
    await s.run_free(session_id)
    assert await answers(s.endpoint), "the guest does not run after the breakpoint arm"
    ended = await s.op("debug.end_session", session_id=session_id)
    s.sessions.remove(session_id)
    assert ended.status == "detached", f"the session ended {ended.status}"
    assert "guest_execution" not in ended.data, "the halt before detach failed"
    assert await answers_again(s.endpoint), "the guest did not answer after a running end"
    return {
        "halted_by": halted_by,
        "reason": reason,
        "breakpoint_path": path,
        "ended_running": True,
    }


async def advance_body(s: Session) -> dict[str, object]:
    """An instruction step lands where the fixture binary says; ``out`` then returns.

    The step's expected landing comes from ``objdump`` of the fixture ``vmlinux``: the next
    instruction, or a transfer's target. ``out`` from ``vfs_read`` stops after one of the
    fixture's ``call vfs_read`` sites in a read(2)-family caller, which must also be frame 1's
    address; from a callee the step entered, at the instruction after the call.
    """
    session_id = await s.start()
    pc = await s.stop_in_vfs_read(session_id)
    frames = await s.frames(session_id)
    assert frames[0].get("func") == BREAK_AT, f"frame 0 is {frames[0].get('func')!r}"
    assert len(frames) > 1, "vfs_read has no caller frame"
    code = await fixture_code(s.tree / "vmlinux", s.truth.at(BREAK_AT), s.truth.end_of(BREAK_AT))
    landings = next_pcs(code, pc)
    stepped = await s.exposed(session_id=session_id, mode="instruction", timeout_sec=_STOP_WAIT_S)
    require_stop(stepped, "end-stepping-range")
    step = await s.register(session_id)
    assert step in landings, "the instruction step did not land where the fixture binary says"
    entered = not s.truth.within(BREAK_AT, step)
    if entered:
        returns = {a for a in landings if s.truth.within(BREAK_AT, a)}
    else:
        returns = await return_sites(s.tree / "vmlinux", s.truth, BREAK_AT)
        assert hex_value(frames[1].get("addr")) in returns, "frame 1 is not after a vfs_read call"
    env = await s.exposed(session_id=session_id, mode="out", timeout_sec=_STOP_WAIT_S)
    require_stop(env, "function-finished")
    rip = await s.register(session_id)
    assert rip in returns, "advance out did not stop at a return address of the fixture binary"
    if not entered:
        assert rip == hex_value(frames[1].get("addr")), "advance out left another frame"
    await s.end(session_id)
    return {
        "modes": ["instruction", "out"],
        "step_matches_fixture": True,
        "entered_callee": entered,
        "out_at_return_address": True,
    }


async def set_breakpoint_body(s: Session) -> dict[str, object]:
    """The installed breakpoint is listed and reached in ``vfs_read``."""
    session_id = await s.start()
    env = await s.exposed(session_id=session_id, location=BREAK_AT)
    number = str(env.data.get("number") or "")
    assert env.status == "set" and number, "no breakpoint number returned"
    assert await s.breakpoints(session_id) == 1, "the breakpoint is not listed"
    require_stop(await s.proceed(session_id), "breakpoint-hit")
    frames = await s.frames(session_id)
    assert frames[0].get("func") == BREAK_AT, f"frame 0 is {frames[0].get('func')!r}"
    assert s.truth.within(BREAK_AT, await s.register(session_id)), "the stop is not in vfs_read"
    await s.op("debug.clear_breakpoint", session_id=session_id, number=number)
    await s.end(session_id)
    return {"listed": 1, "reason": "breakpoint-hit", "frame0": BREAK_AT}


async def clear_breakpoint_body(s: Session) -> dict[str, object]:
    """A cleared ``vfs_read`` breakpoint is gone and no longer stops the read workload."""
    session_id = await s.start()
    number = await s.breakpoint(session_id)
    env = await s.exposed(session_id=session_id, number=number)
    assert env.status == "cleared", f"the clear answered {env.status}"
    assert await s.breakpoints(session_id) == 0, "the breakpoint is still listed"
    require_no_stop(await s.proceed(session_id, _NO_STOP_WAIT_S))
    assert await s.register(session_id) > 0, "the session is unusable after timeout"
    await s.end(session_id)
    return {"listed": 0, "stopped_by_workload": False}


async def list_breakpoints_body(s: Session) -> dict[str, object]:
    """Each installed breakpoint is listed with its number, kind and kallsyms-true location."""
    session_id = await s.start()
    first = await s.breakpoint(session_id)
    second = await s.breakpoint(session_id, "vfs_write")
    both = await s.exposed(session_id=session_id)
    await s.op("debug.clear_breakpoint", session_id=session_id, number=first)
    one_left = await s.exposed(session_id=session_id)
    await s.end(session_id)
    _require_breakpoints(s, both, {first: BREAK_AT, second: "vfs_write"})
    _require_breakpoints(s, one_left, {second: "vfs_write"})
    return {"counts": [2, 1], "numbers_match": True, "addr_within_kallsyms": True}


def _require_breakpoints(s: Session, env: ToolResponse, expected: dict[str, str]) -> None:
    rows = env.data.get("breakpoints")
    assert isinstance(rows, list), "no breakpoints listed"
    seen = {str(row.get("number")): row for row in rows if isinstance(row, dict)}
    numbers = sorted(seen)
    assert env.data.get("count") == len(expected) and numbers == sorted(expected), (
        f"listed {numbers}, expected {sorted(expected)}"
    )
    for number, func in expected.items():
        row = seen[number]
        assert (row.get("type"), row.get("func")) == ("breakpoint", func), (
            f"breakpoint {number} is {row}"
        )
        assert s.truth.within(func, hex_value(row.get("addr"))), (
            f"breakpoint {number} is not in {func}"
        )


async def set_watchpoint_body(s: Session) -> dict[str, object]:
    """The watched ``jiffies_64`` write stops the guest with a larger value."""
    session_id = await s.start()
    before = await s.word(session_id, WATCH)
    env = await s.exposed(session_id=session_id, symbol=WATCH, byte_count=8)
    number = str(env.data.get("number") or "")
    assert env.status == "watching" and number, "no watchpoint number returned"
    expression = f"*(char(*)[8]){s.truth.at(WATCH):#x}"
    assert env.data.get("expr") == expression, "the watchpoint is not on jiffies_64's address"
    stop = await s.op("debug.continue", session_id=session_id, timeout_sec=_STOP_WAIT_S)
    require_stop(stop, "watchpoint-trigger")
    after = await s.word(session_id, WATCH)
    assert after > before, "the watched value did not grow"
    await s.op("debug.clear_watchpoint", session_id=session_id, number=number)
    await s.end(session_id)
    return {"reason": "watchpoint-trigger", "value_increased": True}


async def clear_watchpoint_body(s: Session) -> dict[str, object]:
    """A cleared ``jiffies_64`` watchpoint is gone and no longer stops the ticking guest."""
    session_id = await s.start()
    number = await s.watchpoint(session_id)
    env = await s.exposed(session_id=session_id, number=number)
    assert env.status == "cleared", f"the clear answered {env.status}"
    listed = await s.op("debug.list_watchpoints", session_id=session_id)
    assert listed.data.get("count") == 0, "the watchpoint is still listed"
    stop = await s.op("debug.continue", session_id=session_id, timeout_sec=_NO_STOP_WAIT_S)
    require_no_stop(stop)
    assert await s.register(session_id) > 0, "the session is unusable after timeout"
    await s.end(session_id)
    return {"listed": 0, "stopped_by_tick": False}


async def list_watchpoints_body(s: Session) -> dict[str, object]:
    """The one installed watchpoint is listed with its number and expression."""
    session_id = await s.start()
    number = await s.watchpoint(session_id)
    env = await s.exposed(session_id=session_id)
    listed = env.data.get("watchpoints")
    assert isinstance(listed, list), "no watchpoints listed"
    seen = [(str(w.get("number")), w.get("expr")) for w in listed if isinstance(w, dict)]
    expression = f"*(char(*)[8]){s.truth.at(WATCH):#x}"
    assert (env.data.get("count"), seen) == (1, [(number, expression)]), f"listed {seen}"
    await s.op("debug.clear_watchpoint", session_id=session_id, number=number)
    await s.end(session_id)
    return {"count": 1, "number_matches": True, "expr_at_kallsyms_address": True}


async def read_memory_body(s: Session) -> dict[str, object]:
    """``linux_banner``'s bytes are the guest's ``/proc/version``."""
    session_id = await s.start()
    size = len(s.truth.version)
    env = await s.exposed(session_id=session_id, address=s.truth.at(BANNER), byte_count=size)
    await s.end(session_id)
    read = bytes.fromhex(str(env.data.get("memory_hex")))
    assert read == s.truth.version, "linux_banner differs from /proc/version"
    assert read.startswith(f"Linux version {s.manifest['release']} ".encode()), "not the fixture"
    return {"bytes": size, "sha256": hashlib.sha256(read).hexdigest(), "matches": True}


async def read_registers_body(s: Session) -> dict[str, object]:
    """Stopped in ``vfs_read``: ``rip`` lies in it and ``rsp`` is a kernel stack address."""
    session_id = await s.start()
    await s.stop_in_vfs_read(session_id)
    env = await s.exposed(session_id=session_id, registers=["rip", "rsp"])
    await s.end(session_id)
    rip, rsp = hex_value(env.data.get("rip")), hex_value(env.data.get("rsp"))
    assert s.truth.within(BREAK_AT, rip), "rip is not in vfs_read"
    assert rsp >= KERNEL_BASE, "rsp is not a kernel address"
    return {"rip_in": BREAK_AT, "rsp_kernel": True}


async def resolve_body(s: Session) -> dict[str, object]:
    """Resolved addresses equal the guest's ``/proc/kallsyms``."""
    session_id = await s.start()
    resolved = {}
    for name in (BREAK_AT, BANNER):
        env = await s.exposed(session_id=session_id, name=name)
        resolved[name] = hex_value(env.data.get("address")) == s.truth.at(name)
    await s.end(session_id)
    assert all(resolved.values()), f"resolved addresses differ from kallsyms: {resolved}"
    return {"matches": sorted(resolved)}


async def disassemble_body(s: Session) -> dict[str, object]:
    """Sixteen instructions from ``vfs_read`` on the fixture binary's instruction boundaries.

    Mnemonics are recorded, not required: the kernel patches some sites at boot (the ftrace
    call, static branches), so live text may differ from the binary at an unchanged boundary.
    """
    session_id = await s.start()
    env = await s.exposed(session_id=session_id, symbol=BREAK_AT, instruction_count=16)
    await s.end(session_id)
    listed = env.data.get("instructions")
    assert isinstance(listed, list) and len(listed) == 16, "not 16 instructions"
    rows = [cast(dict[str, Any], i) for i in listed]
    code = await fixture_code(s.tree / "vmlinux", s.truth.at(BREAK_AT), s.truth.end_of(BREAK_AT))
    assert len(code) >= 16, "the fixture's vfs_read has fewer than 16 instructions"
    addresses = [hex_value(row.get("address")) for row in rows]
    assert addresses == [ins.address for ins in code[:16]], "boundaries differ from the binary"
    assert {row.get("func_name") for row in rows} == {BREAK_AT}, "instructions name another func"
    agreeing = sum(
        str(row.get("inst", "")).split()[:1] == [ins.mnemonic]
        for row, ins in zip(rows, code, strict=False)
    )
    return {"instructions": 16, "boundaries_match_fixture": True, "mnemonics_agreeing": agreeing}


async def backtrace_body(s: Session) -> dict[str, object]:
    """The read workload's stop walks from ``vfs_read`` up through the read syscall."""
    session_id = await s.start()
    rip = await s.stop_in_vfs_read(session_id)
    env = await s.exposed(session_id=session_id)
    await s.end(session_id)
    listed = env.data.get("frames")
    assert isinstance(listed, list) and listed, "the backtrace has no frames"
    funcs = [cast(dict[str, Any], f).get("func") for f in listed]
    top = cast(dict[str, Any], listed[0])
    assert funcs[0] == BREAK_AT and hex_value(top.get("addr")) == rip, "frame 0 differs"
    caller = funcs[1] if len(funcs) > 1 else None
    assert caller in _READ_CALLERS, f"vfs_read's caller is {caller!r}"
    assert any(f and "syscall" in f for f in funcs), "the walk does not reach the syscall entry"
    return {"frames": len(funcs), "frame0": BREAK_AT, "caller": caller}


def _number(value: object, name: str) -> int:
    token = str(value).split(" ", 1)[0]
    assert re.fullmatch(r"0x[0-9a-f]+|[0-9]+", token), f"vfs_read's {name} reads {value!r}"
    return int(token, 0)


def _variables(frame: Mapping[str, object], key: str) -> list[dict[str, Any]]:
    listed = frame.get(key)
    assert isinstance(listed, list), f"the frame has no {key}"
    return [cast(dict[str, Any], v) for v in listed]


async def read_frame_body(s: Session) -> dict[str, object]:
    """Frame 0 is ``vfs_read`` at its entry, its arguments the ABI registers and ``ret`` a local.

    gdb adds a ``<name>@entry`` row for a parameter whose entry value DWARF recovers; at the entry
    stop it must equal the same register as the parameter itself.
    """
    session_id = await s.start()
    rip = await s.stop_in_vfs_read(session_id)
    assert rip == s.truth.at(BREAK_AT), "the stop is not at vfs_read's entry"
    registers = [await s.register(session_id, name) for name in _ARG_REGISTERS]
    env = await s.exposed(session_id=session_id, level=0)
    await s.end(session_id)
    frame = env.data.get("frame")
    assert isinstance(frame, dict), "no frame returned"
    assert (frame.get("func"), hex_value(frame.get("addr"))) == (BREAK_AT, rip), "frame differs"
    rows = {str(v.get("name")): v.get("value") for v in _variables(frame, "args")}
    declared = tuple(name for name in rows if not name.endswith("@entry"))
    assert declared == _VFS_READ_ARGS, f"vfs_read's arguments are {declared}"
    for name, value in rows.items():
        parameter = name.removesuffix("@entry")
        assert parameter in _VFS_READ_ARGS, f"{name} is not a vfs_read parameter"
        index = _VFS_READ_ARGS.index(parameter)
        assert _number(value, name) == registers[index], f"{name} is not {_ARG_REGISTERS[index]}"
    assert 0 < registers[1] < KERNEL_BASE, "buf is not a user pointer"
    assert "ret" in [v.get("name") for v in _variables(frame, "locals")], "no ret local"
    entry_values = len(rows) - len(declared)
    return {
        "frame0": BREAK_AT,
        "args": list(_VFS_READ_ARGS),
        "entry_values": entry_values,
        "registers_match": True,
    }


def _module_row(env: ToolResponse) -> dict[str, Any]:
    listed = env.data.get("modules")
    assert isinstance(listed, list), "no modules listed"
    rows = [cast(dict[str, Any], m) for m in listed if isinstance(m, dict)]
    found = [m for m in rows if m.get("name") == MODULE]
    assert len(found) == 1, f"{MODULE} is listed {len(found)} times"
    return found[0]


async def list_modules_body(s: Session) -> dict[str, object]:
    """``loop`` is listed at its ``/sys/module`` ``.text`` base, symbols not yet loaded."""
    session_id = await s.start()
    env = await s.exposed(session_id=session_id)
    await s.end(session_id)
    row = _module_row(env)
    assert hex_value(row.get("base_address")) == s.truth.loop_text, "loop's base differs"
    assert row.get("symbols_loaded") is False, "loop's symbols were already loaded"
    return {"module": MODULE, "base_matches": True}


async def load_module_symbols_body(s: Session) -> dict[str, object]:
    """After the load, a ``loop`` function resolves to its ``/proc/kallsyms`` address."""
    expected = s.truth.module.get(MODULE_SYMBOL)
    assert expected, f"/proc/kallsyms lists no [{MODULE}] {MODULE_SYMBOL}"
    session_id = await s.start()
    before = await s.raw("debug.resolve_symbol", session_id=session_id, name=MODULE_SYMBOL)
    assert before.error_category == ErrorCategory.SYMBOL_NOT_FOUND.value, (
        f"{MODULE_SYMBOL} resolved before its module's symbols were loaded"
    )
    env = await s.exposed(session_id=session_id, module=MODULE, expected_base=s.truth.loop_text)
    after = await s.op("debug.resolve_symbol", session_id=session_id, name=MODULE_SYMBOL)
    await s.end(session_id)
    assert env.status == "loaded" and env.data.get("symbols_loaded") is True, "not loaded"
    assert hex_value(env.data.get("base_address")) == s.truth.loop_text, "loaded at another base"
    assert hex_value(after.data.get("address")) == expected, f"{MODULE_SYMBOL} resolved elsewhere"
    return {"module": MODULE, "symbol": MODULE_SYMBOL, "resolves_to_kallsyms": True}


BODIES: dict[str, Callable[[Session], Awaitable[dict[str, object]]]] = {
    "debug.advance": advance_body,
    "debug.backtrace": backtrace_body,
    "debug.clear_breakpoint": clear_breakpoint_body,
    "debug.clear_watchpoint": clear_watchpoint_body,
    "debug.continue": continue_body,
    "debug.disassemble": disassemble_body,
    "debug.end_session": end_body,
    "debug.get_session": get_body,
    "debug.interrupt": interrupt_body,
    "debug.list_breakpoints": list_breakpoints_body,
    "debug.list_modules": list_modules_body,
    "debug.list_sessions": list_body,
    "debug.list_watchpoints": list_watchpoints_body,
    "debug.load_module_symbols": load_module_symbols_body,
    "debug.read_frame": read_frame_body,
    "debug.read_memory": read_memory_body,
    "debug.read_registers": read_registers_body,
    "debug.resolve_symbol": resolve_body,
    "debug.set_breakpoint": set_breakpoint_body,
    "debug.set_watchpoint": set_watchpoint_body,
    "debug.start_session": start_body,
}


async def functional(
    run: CellRun,
    caller: HttpCaller,
    tree: Path,
    manifest: Mapping[str, Any],
    db_url: str,
    guest: Guest,
) -> dict[str, object]:
    """Boot the fixture, read the guest's truth, then run the tool's sequence as its gate role.

    Every session the body started and still holds is ended, also on failure, before the
    frame's cleanup probes the System.
    """
    run_id, endpoint = await boot(run, guest, tree, manifest)
    truth = await read_truth(endpoint, guest.key, tree)
    role = grants(guest.project, GATES[run.cell.operation])
    session = Session(run, guest, caller, role, manifest, run_id, endpoint, truth, db_url, tree)
    try:
        observed = await BODIES[run.cell.operation](session)
    finally:
        await session.end_all()
    return {"tool": run.cell.operation, **observed}


# --- rejection cells ------------------------------------------------------------------------


@dataclass(frozen=True)
class DebugTarget:
    """A torn-down fixture System, its Run and an ended gdbstub session."""

    project: str
    system_id: str
    run_id: str
    session_id: str
    observed: dict[str, object]
    artifacts: tuple[str, ...]


@dataclass(frozen=True)
class _Failed:
    error: Exception
    artifacts: tuple[str, ...]


_TARGETS: dict[str, DebugTarget | _Failed] = {}


async def _prepare_target(
    run: CellRun,
    base_url: str,
    issuer: OidcIssuer,
    db_url: str,
    fixture: Callable[[], tuple[Path, dict[str, Any]]],
    scratch: CellRun,
) -> DebugTarget:
    tree, manifest = fixture()
    project = f"cov-{secrets.token_hex(4)}"
    ids: dict[str, str] = {}

    async def body(guest: Guest) -> dict[str, object]:
        run_id, _endpoint = await boot(scratch, guest, tree, manifest)
        started = checked(
            await scalar(guest.op, "debug.start_session", run_id=run_id, transport="gdbstub"),
            "target-start",
        )
        try:
            checked(
                await scalar(
                    guest.op,
                    "debug.read_registers",
                    session_id=started.object_id,
                    registers=["rip"],
                ),
                "target-attach",
            )
        finally:
            ended = ok(
                await scalar(guest.op, "debug.end_session", session_id=started.object_id),
                "target-end",
            )
        assert ended.status == "detached", f"the target session ended {ended.status}"
        ids.update(system=guest.system_id, run=run_id, session=started.object_id)
        return {"debug_target": "prepared"}

    await on_lane_system(
        scratch,
        base_url,
        issuer,
        db_url,
        project=project,
        body=body,
        provision=provision_gdbstub,
    )
    await settled(db_url, project)
    summary = run.writer.artifact(
        {
            "debug_target": {"system": "torn_down", "run": "booted", "session": "detached"},
            "cleanup": scratch.assertions["cleanup"],
        }
    )
    return DebugTarget(
        project,
        ids["system"],
        ids["run"],
        ids["session"],
        lane_keys(scratch.observed),
        (*scratch.artifacts, *scratch.assertions.values(), summary),
    )


async def debug_target(
    run: CellRun,
    base_url: str,
    issuer: OidcIssuer,
    db_url: str,
    fixture: Callable[[], tuple[Path, dict[str, Any]]],
) -> DebugTarget:
    """The stack's rejection target, prepared on first use; a failure is replayed, not retried.

    The fixture boot runs on a scratch record, so the cached context keeps only the lane keys.
    """
    cached = _TARGETS.get(base_url)
    if cached is None:
        scratch = CellRun(run.cell, run.writer)
        try:
            cached = await _prepare_target(run, base_url, issuer, db_url, fixture, scratch)
        except Exception as exc:  # noqa: BLE001 - remembered and replayed for every cell
            cached = _Failed(exc, (*scratch.artifacts, *scratch.assertions.values()))
        _TARGETS[base_url] = cached
    if isinstance(cached, _Failed):
        run.artifacts.extend(cached.artifacts)
        if isinstance(cached.error, ScenarioStop):
            raise ScenarioStop(cached.error.outcome, str(cached.error))
        raise AssertionError(f"the debug target could not be prepared: {cached.error!r}")
    run.observed |= cached.observed
    run.artifacts.extend(cached.artifacts)
    return cached


def _stranger() -> Grants:
    other = f"cov-{secrets.token_hex(4)}"
    return grants(other, "admin")


def _args(tool: str, target: DebugTarget, variant: str, ident: object) -> dict[str, object]:
    """The tool's call naming ``ident`` as its id, with fixed valid other arguments."""
    if tool == _LIST:
        return {"request": {"run_id": ident}}
    if tool == "debug.start_session":
        return {"run_id": ident, "transport": variant}
    return {"session_id": ident, **_EXTRA.get(tool, {})}


def rejection(tool: str, boundary: Boundary, target: DebugTarget, variant: str) -> Rejection:
    """The call ``boundary`` must reject, aimed at the debug target.

    ``variant`` is the cell's transport for ``debug.start_session``. ``debug.list_sessions``
    filters its denied callers, so its authorization and project-isolation calls carry a viewer
    control that must see the target session (ADR-0756).
    """
    own = target.run_id if tool in (_LIST, "debug.start_session") else target.session_id
    args = _args(tool, target, variant, own)
    gate = GATES[tool]
    if boundary == "validation":
        return Rejection(_args(tool, target, variant, 7), grants(target.project, gate))
    if boundary == "authentication":
        return Rejection(args, grants(target.project, "viewer"))
    control = FilteredListControl(grants(target.project, "viewer"), partial(_known, target))
    if boundary == "authorization":
        denied = grants(target.project, _BELOW[gate])
        return Rejection(args, denied, filtered=control if tool == _LIST else None)
    assert boundary == "project-isolation", f"no {boundary} cell exists for {tool}"
    if tool == _LIST:
        return Rejection(args, _stranger(), filtered=control)
    category = (
        ErrorCategory.NOT_FOUND
        if tool == "debug.get_session"
        else ErrorCategory.CONFIGURATION_ERROR
    )
    twin = _args(tool, target, variant, str(uuid4()))
    return Rejection(args, _stranger(), frozenset({category.value}), absent_twin=twin)


async def _known(target: DebugTarget) -> str:
    """The target session, known from its preparation rather than from any list call."""
    return target.session_id


def snapshot_of(db_url: str, target: DebugTarget) -> Snapshot:
    return partial(protected_state, db_url, target.project, target.system_id, target.run_id)
