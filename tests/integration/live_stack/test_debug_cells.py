"""Unit tests of the local debug cell oracles, bodies and rejections (no stack needed)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from kdive.domain.errors import ErrorCategory
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.debug.operations.runtime import _op_failure
from kdive.providers.shared.debug_common.gdbmi.core.engine import GdbMiEngine
from scripts.coverage_campaign.contract import FILTERED_LIST_BOUNDARIES, Cell, build_contract
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack import debug_cells
from tests.integration.live_stack.debug_cells import (
    BODIES,
    BREAK_AT,
    TOOLS,
    DebugTarget,
    Instruction,
    Session,
    Truth,
    checked,
    gdbstub_profile,
    hex_value,
    next_pcs,
    parse_kallsyms,
    parse_objdump,
    rejection,
    system_map_address,
)
from tests.integration.live_stack.image_smoke import Endpoint
from tests.integration.live_stack.scenario import CellRun, ScenarioStop
from tests.integration.live_stack.tool_cells import Grants, Guest, HttpCaller, boundary_of

_TARGET = DebugTarget("cov-t", "sys-1", "run-1", "ses-1", {}, ())
_KALLSYMS = """\
ffffffff81000000 T _stext
ffffffff81200000 T vfs_read
ffffffff81200400 t vfs_read.cold
ffffffff81200800 T vfs_write
ffffffff81300000 T ksys_read
ffffffff81300100 T ksys_write
ffffffff82000000 D linux_banner
ffffffff82100000 D jiffies_64
ffffffffc0001000 t lo_release\t[loop]
ffffffffc0001200 t loop_queue_rq\t[loop]
ffffffffc0002000 t other_fn\t[other]
"""


def _truth() -> Truth:
    core, module, text = parse_kallsyms(_KALLSYMS)
    return Truth(b"Linux version 6.18.54-kdive (b@h) #1\n", core, module, text, 0xFFFFFFFFC0001000)


def test_parse_kallsyms_splits_core_and_module_text() -> None:
    core, module, text = parse_kallsyms(_KALLSYMS)
    assert core["vfs_read"] == 0xFFFFFFFF81200000 and core["linux_banner"] == 0xFFFFFFFF82000000
    assert module == {"lo_release": 0xFFFFFFFFC0001000, "loop_queue_rq": 0xFFFFFFFFC0001200}
    assert 0xFFFFFFFF82000000 not in text and text == tuple(sorted(text))


def test_parse_kallsyms_refuses_a_pointer_restricted_listing() -> None:
    with pytest.raises(AssertionError, match="pointer restriction"):
        parse_kallsyms("0000000000000000 T vfs_read\n0000000000000000 T vfs_write\n")


def test_truth_bounds_a_function_by_the_next_text_symbol() -> None:
    truth = _truth()
    assert truth.end_of("vfs_read") == 0xFFFFFFFF81200400
    assert truth.within("vfs_read", 0xFFFFFFFF812003FF)
    assert not truth.within("vfs_read", 0xFFFFFFFF81200400)
    assert not truth.within("vfs_read", 0xFFFFFFFF811FFFFF)
    with pytest.raises(AssertionError, match="names no absent"):
        truth.at("absent")


def test_system_map_address_and_hex_values() -> None:
    assert system_map_address("ffffffff81200000 T vfs_read\n", "vfs_read") == 0xFFFFFFFF81200000
    with pytest.raises(AssertionError):
        system_map_address("ffffffff81200000 T vfs_write\n", "vfs_read")
    assert hex_value("0xffffffff81200004 <vfs_read+4>") == 0xFFFFFFFF81200004
    with pytest.raises(AssertionError):
        hex_value(17)


def test_gdbstub_profile_adds_only_the_stub() -> None:
    profile = {"arch": "x86_64", "provider": {"local-libvirt": {"vcpus": 2}}}
    edited = gdbstub_profile(profile)
    assert edited["provider"] == {"local-libvirt": {"vcpus": 2, "debug": {"gdbstub": True}}}
    assert profile["provider"] == {"local-libvirt": {"vcpus": 2}}


@pytest.mark.parametrize("cross", [False, True], ids=["native", "cross-arch"])
def test_a_missing_debugger_or_dependency_blocks(cross: bool) -> None:
    # The envelope the op path really answers: the engine's error through its failure mapping.
    error = GdbMiEngine()._missing_gdb_error(is_cross_arch=cross, guest_arch="ppc64le")
    gdb = _op_failure("s", error)
    for missing in (gdb, ToolResponse.failure("s", ErrorCategory.MISSING_DEPENDENCY, detail="x")):
        with pytest.raises(ScenarioStop) as stop:
            checked(missing, "attach")
        assert stop.value.outcome is Outcome.BLOCKED
    with pytest.raises(AssertionError):
        checked(ToolResponse.failure("s", ErrorCategory.DEBUG_ATTACH_FAILURE, detail="x"), "a")


def _cells() -> list[Cell]:
    return [
        c
        for c in build_contract().cells
        if c.operation in TOOLS and c.provider == "local-libvirt" and c.guest_arch == "x86_64"
    ]


def test_rejection_covers_every_boundary_of_every_tool() -> None:
    cells = [c for c in _cells() if c.kind == "rejection"]
    assert not [c for c in cells if c.id.endswith("/capability")]
    pairs = {(c.operation, boundary_of(c), c.scenario_id.split("/")[2]) for c in cells}
    assert {tool for tool, _, _ in pairs} == set(TOOLS)
    for tool, boundary, variant in pairs:
        call = rejection(tool, boundary, _TARGET, variant)
        if tool == "debug.list_sessions":
            ident = cast(Mapping[str, object], call.args["request"])["run_id"]
        else:
            ident = call.args["run_id" if tool == "debug.start_session" else "session_id"]
        own = "run-1" if tool in ("debug.list_sessions", "debug.start_session") else "ses-1"
        assert ident == (7 if boundary == "validation" else own)
        if tool == "debug.start_session":
            assert call.args["transport"] == variant
        role = call.grants.roles.get("cov-t")
        filtered = (tool, boundary) in FILTERED_LIST_BOUNDARIES
        assert (call.filtered is not None) == filtered
        if boundary == "authentication":
            assert role == "viewer"
        if boundary == "authorization":
            assert role == (
                None if tool in ("debug.get_session", "debug.list_sessions") else "viewer"
            )
            assert "cov-t" in call.grants.projects
        if boundary == "project-isolation":
            assert "cov-t" not in call.grants.projects
            assert set(call.grants.roles.values()) == {"admin"}
        if boundary == "project-isolation" and not filtered:
            assert call.absent_twin is not None and call.absent_twin != call.args
            expected = "not_found" if tool == "debug.get_session" else "configuration_error"
            assert call.categories == {expected}


def test_list_sessions_control_is_a_viewer_who_knows_the_target_session() -> None:
    for boundary in ("authorization", "project-isolation"):
        control = rejection("debug.list_sessions", boundary, _TARGET, "gdbstub").filtered
        assert control is not None and control.grants.roles == {"cov-t": "viewer"}
        assert asyncio.run(control.expected_item()) == "ses-1"


# --- bodies against a scripted debugger -----------------------------------------------------

_RIP = "0xffffffff81200010"
_RETURN = "0xffffffff81300020"
_Answer = Callable[[Mapping[str, object]], ToolResponse]


def _ok(status: str, **data: object) -> _Answer:
    return lambda _args: ToolResponse.success("ses-1", status, data=cast(Any, data))


@dataclass
class _Debugger:
    """The operator client and the cell's caller over one scripted debugger."""

    answers: dict[str, _Answer]
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    started: int = 0

    def answer(self, tool: str, args: Mapping[str, object]) -> ToolResponse:
        self.calls.append((tool, dict(args)))
        if tool == "debug.start_session":
            self.started += 1
            return ToolResponse.success(f"ses-{self.started}", "live")
        if tool == "debug.end_session":
            return ToolResponse.success(str(args["session_id"]), "detached")
        return self.answers[tool](args)

    async def call_tool(self, tool: str, /, **args: object) -> ToolResponse:
        return self.answer(tool, args)

    def token(self, grants: Grants) -> str:
        return "t"

    async def call(
        self, tool: str, args: Mapping[str, object], token: str, *, discover: bool = False
    ) -> ToolResponse:
        return self.answer(tool, args)


def _session(tool: str, debugger: _Debugger) -> Session:
    cell = SimpleNamespace(operation=tool, scenario_id=f"tool/{tool}/gdbstub/functional")
    run = cast(CellRun, SimpleNamespace(cell=cell))
    guest = cast(Guest, SimpleNamespace(op=debugger, project="p", system_id="sys-1"))
    return Session(
        run,
        guest,
        cast(HttpCaller, debugger),
        Grants("s", ("p",)),
        {"release": "6.18.54-kdive"},
        "run-1",
        Endpoint("127.0.0.1", 1),
        _truth(),
        "postgresql://unused",
        Path("/fixture"),
    )


def _stopped(reason: str | None, *, timed_out: bool = False) -> _Answer:
    data: dict[str, object] = {"timed_out": timed_out}
    if reason is not None:
        data["reason"] = reason
    return lambda _args: ToolResponse.success("ses-1", "stopped", data=cast(Any, data))


def _frames(*funcs: tuple[str, str]) -> _Answer:
    frames = [{"level": i, "func": f, "addr": a} for i, (f, a) in enumerate(funcs)]
    return _ok("walked", frames=frames, frame_count=len(frames), truncated=False)


def _stop_answers(**extra: _Answer) -> dict[str, _Answer]:
    return {
        "debug.set_breakpoint": _ok("set", number="1"),
        "debug.clear_breakpoint": _ok("cleared"),
        "debug.continue": _stopped("breakpoint-hit"),
        "debug.read_registers": _ok("read", rip=_RIP, rsp="0xffffc90000a3fe00"),
        "debug.backtrace": _frames(
            (BREAK_AT, _RIP), ("ksys_read", _RETURN), ("do_syscall_64", "0xffffffff81900000")
        ),
        **extra,
    }


def _run(tool: str, debugger: _Debugger, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    async def resumed(_self: Session, call: Any) -> ToolResponse:
        return cast(ToolResponse, await call)

    monkeypatch.setattr(Session, "resumed", resumed)
    session = _session(tool, debugger)

    async def body() -> dict[str, object]:
        try:
            return await BODIES[tool](session)
        finally:
            await session.end_all()

    return asyncio.run(body())


_BASE = 0xFFFFFFFF81200000
_CODE = [Instruction(_BASE + 4 * i, "push   %rbp" if i == 4 else "nop", None) for i in range(16)]


# ksys_read calls vfs_read at +0x1b and resumes at +0x20, the scripted frame 1 address.
_CALLER = [
    Instruction(0xFFFFFFFF81300000, "endbr64", None),
    Instruction(0xFFFFFFFF8130001B, "call   ffffffff81200000 <vfs_read>", _BASE),
    Instruction(0xFFFFFFFF81300020, "mov    %rax,%rbx", None),
]


def _fixture_code(monkeypatch: pytest.MonkeyPatch, code: list[Instruction] = _CODE) -> None:
    async def fixture_code(_vmlinux: Path, start: int, _stop: int) -> list[Instruction]:
        return {_BASE: code, 0xFFFFFFFF81300000: _CALLER}[start]

    monkeypatch.setattr(debug_cells, "fixture_code", fixture_code)


def _stepping(step_to: int, out_to: int) -> dict[str, _Answer]:
    landed = {"rip": _RIP}

    def registers(_args: Mapping[str, object]) -> ToolResponse:
        return ToolResponse.success("ses-1", "read", data=cast(Any, dict(landed)))

    def advance(args: Mapping[str, object]) -> ToolResponse:
        out = args["mode"] == "out"
        landed["rip"] = hex(out_to if out else step_to)
        return _stopped("function-finished" if out else "end-stepping-range")(args)

    return _stop_answers(**{"debug.read_registers": registers, "debug.advance": advance})


def test_advance_steps_to_the_fixture_boundary_then_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixture_code(monkeypatch)
    good = _Debugger(_stepping(_BASE + 0x14, int(_RETURN, 16)))
    observed = _run("debug.advance", good, monkeypatch)
    assert observed["step_matches_fixture"] is True and observed["entered_callee"] is False
    modes = [args["mode"] for tool, args in good.calls if tool == "debug.advance"]
    assert modes == ["instruction", "out"]

    skipped = _Debugger(_stepping(_BASE + 0x18, int(_RETURN, 16)))
    with pytest.raises(AssertionError, match="where the fixture binary says"):
        _run("debug.advance", skipped, monkeypatch)
    assert ("debug.end_session", {"session_id": "ses-1"}) in skipped.calls
    wrong_out = _Debugger(_stepping(_BASE + 0x14, _BASE + 0x20))
    with pytest.raises(AssertionError, match="return address"):
        _run("debug.advance", wrong_out, monkeypatch)
    # A frame 1 the binary does not call vfs_read from is caught before the step.
    frames = _frames((BREAK_AT, _RIP), ("ksys_read", "0xffffffff81300024"))
    bad_frame = _Debugger(
        {**_stepping(_BASE + 0x14, 0xFFFFFFFF81300024), "debug.backtrace": frames}
    )
    with pytest.raises(AssertionError, match="not after a vfs_read call"):
        _run("debug.advance", bad_frame, monkeypatch)


def test_next_pcs_follows_the_fixture_instruction() -> None:
    call = Instruction(_BASE + 0x10, "call   ffffffff81300000 <rw_verify_area>", 0xFFFFFFFF81300000)
    code = [*_CODE[:4], call, *_CODE[5:]]
    assert next_pcs(_CODE, _BASE + 0x10) == {_BASE + 0x14}
    assert next_pcs(code, _BASE + 0x10) == {_BASE + 0x14, 0xFFFFFFFF81300000}
    with pytest.raises(AssertionError, match="not an instruction boundary"):
        next_pcs(_CODE, _BASE + 0x11)
    ret = [*_CODE[:4], Instruction(_BASE + 0x10, "ret", None), *_CODE[5:]]
    with pytest.raises(AssertionError, match="cannot predict"):
        next_pcs(ret, _BASE + 0x10)


def test_parse_objdump_reads_addresses_text_and_targets() -> None:
    text = (
        "ffffffff81200000 <vfs_read>:\n"
        "ffffffff81200000:\tendbr64\n"
        "ffffffff81200004:\tcall   ffffffff81000100 <__fentry__>\n"
        "ffffffff81200009:\tjne    ffffffff81200040 <vfs_read+0x40>\n"
    )
    code = parse_objdump(text)
    assert [i.address for i in code] == [0xFFFFFFFF81200000, 0xFFFFFFFF81200004, 0xFFFFFFFF81200009]
    assert code[1].target == 0xFFFFFFFF81000100 and code[1].transfers()
    assert code[2].mnemonic == "jne" and not code[0].transfers()


def test_disassemble_needs_the_fixture_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixture_code(monkeypatch)
    rows = [{"address": hex(i.address), "inst": i.text, "func_name": BREAK_AT} for i in _CODE]
    good = {"debug.disassemble": _ok("disassembled", instructions=rows, instruction_count=16)}
    observed = _run("debug.disassemble", _Debugger(good), monkeypatch)
    assert observed["mnemonics_agreeing"] == 16
    shifted = [{**r, "address": hex(hex_value(r["address"]) + 1)} for r in rows]
    bad = {"debug.disassemble": _ok("disassembled", instructions=shifted, instruction_count=16)}
    with pytest.raises(AssertionError, match="boundaries differ"):
        _run("debug.disassemble", _Debugger(bad), monkeypatch)


def test_list_watchpoints_compares_the_address_expression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def listed(expr: str) -> dict[str, _Answer]:
        rows = [{"number": "2", "type": "hw watchpoint", "expr": expr}]
        return {
            "debug.set_watchpoint": _ok("watching", number="2"),
            "debug.clear_watchpoint": _ok("cleared"),
            "debug.list_watchpoints": _ok("listed", count=1, watchpoints=rows),
        }

    good = listed("*(char(*)[8])0xffffffff82100000")
    assert _run("debug.list_watchpoints", _Debugger(good), monkeypatch)["count"] == 1
    with pytest.raises(AssertionError, match="listed"):
        _run("debug.list_watchpoints", _Debugger(listed("jiffies_64")), monkeypatch)


def _interrupted(halted_by: str, reason: str | None) -> _Answer:
    data: dict[str, object] = {"timed_out": False, "halted_by": halted_by}
    if reason is not None:
        data["reason"] = reason
    return lambda _args: ToolResponse.success("ses-1", "stopped", data=cast(Any, data))


def _background(reply_stops: bool = False) -> _Answer:
    """``debug.continue``: a non-waiting one runs, except that the breakpoint arm's (the second)
    carries the hit in its reply when ``reply_stops``; a waiting one reports the hit."""
    background: list[Mapping[str, object]] = []

    def answer(args: Mapping[str, object]) -> ToolResponse:
        if args.get("wait") is not False:
            return _stopped("breakpoint-hit")(args)
        background.append(args)
        if reply_stops and len(background) == 2:
            return _stopped("breakpoint-hit")(args)
        return ToolResponse.success("ses-1", "running")

    return answer


def _guest_answers(monkeypatch: pytest.MonkeyPatch, *states: bool) -> None:
    replies = iter(states)

    async def answers(_endpoint: Endpoint, timeout: float = 5.0) -> bool:
        return next(replies)

    async def answers_again(_endpoint: Endpoint) -> bool:
        return True

    monkeypatch.setattr(debug_cells, "answers", answers)
    monkeypatch.setattr(debug_cells, "answers_again", answers_again)


@pytest.mark.parametrize(
    ("reply_stops", "path"), [(False, "waiting_continue"), (True, "resume_reply")]
)
def test_interrupt_halts_a_guest_left_running(
    reply_stops: bool, path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _guest_answers(monkeypatch, True, False, True)
    answers = _stop_answers(
        **{
            "debug.continue": _background(reply_stops),
            "debug.interrupt": _interrupted("interrupt", "signal-received"),
        }
    )
    debugger = _Debugger(answers)
    observed = _run("debug.interrupt", debugger, monkeypatch)
    assert observed == {
        "halted_by": "interrupt",
        "reason": "signal-received",
        "breakpoint_path": path,
        "ended_running": True,
    }
    continues = [args for tool, args in debugger.calls if tool == "debug.continue"]
    assert [c.get("wait") for c in continues][0] is False
    assert debugger.calls[-1][0] == "debug.end_session"


@pytest.mark.parametrize(
    ("halted_by", "reason"),
    [("already_halted", None), ("attach", None), ("stop_event", "breakpoint-hit")],
)
def test_interrupt_needs_its_own_halt(
    halted_by: str, reason: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _guest_answers(monkeypatch, True, False, True)
    answers = _stop_answers(
        **{"debug.continue": _background(), "debug.interrupt": _interrupted(halted_by, reason)}
    )
    with pytest.raises(AssertionError, match="the interrupt reported"):
        _run("debug.interrupt", _Debugger(answers), monkeypatch)


def test_interrupt_needs_the_guest_running_between_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _guest_answers(monkeypatch, False)
    answers = _stop_answers(
        **{
            "debug.continue": _background(),
            "debug.interrupt": _interrupted("interrupt", "signal-received"),
        }
    )
    with pytest.raises(AssertionError, match="does not run between calls"):
        _run("debug.interrupt", _Debugger(answers), monkeypatch)


def test_interrupt_needs_a_clean_running_end(monkeypatch: pytest.MonkeyPatch) -> None:
    _guest_answers(monkeypatch, True, False, True)

    class _Unknown(_Debugger):
        def answer(self, tool: str, args: Mapping[str, object]) -> ToolResponse:
            if tool == "debug.end_session":
                self.calls.append((tool, dict(args)))
                return ToolResponse.success(
                    str(args["session_id"]), "detached", data={"guest_execution": "unknown"}
                )
            return super().answer(tool, args)

    answers = _stop_answers(
        **{
            "debug.continue": _background(),
            "debug.interrupt": _interrupted("interrupt", "signal-received"),
        }
    )
    with pytest.raises(AssertionError, match="halt before detach failed"):
        _run("debug.interrupt", _Unknown(answers), monkeypatch)


def test_continue_needs_a_breakpoint_hit_in_vfs_read(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run("debug.continue", _Debugger(_stop_answers()), monkeypatch)["rip_in"] == BREAK_AT
    elsewhere = _ok("read", rip="0xffffffff81200800")
    outside = _Debugger(_stop_answers(**{"debug.read_registers": elsewhere}))
    with pytest.raises(AssertionError, match="not in vfs_read"):
        _run("debug.continue", outside, monkeypatch)
    timed = _Debugger(_stop_answers(**{"debug.continue": _stopped(None, timed_out=True)}))
    with pytest.raises(AssertionError, match="timed out"):
        _run("debug.continue", timed, monkeypatch)


def test_clear_breakpoint_needs_the_workload_to_run_on(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = _stop_answers(
        **{
            "debug.list_breakpoints": _ok("listed", count=0),
            "debug.continue": _stopped("signal-received", timed_out=True),
        }
    )
    assert _run("debug.clear_breakpoint", _Debugger(answers), monkeypatch)["listed"] == 0
    answers["debug.continue"] = _stopped("breakpoint-hit")
    with pytest.raises(AssertionError, match="before its timeout"):
        _run("debug.clear_breakpoint", _Debugger(answers), monkeypatch)


def _listing(*rows: tuple[str, str, str]) -> ToolResponse:
    listed = [{"number": n, "type": "breakpoint", "func": f, "addr": a} for n, f, a in rows]
    return _ok("listed", count=len(listed), breakpoints=listed)({})


_VFS_WRITE = ("2", "vfs_write", "0xffffffff81200810")


def _list_breakpoints(*listings: ToolResponse) -> _Debugger:
    numbers = iter(("1", "2"))
    answers = iter(listings)
    return _Debugger(
        _stop_answers(
            **{
                "debug.set_breakpoint": lambda _a: _ok("set", number=next(numbers))(_a),
                "debug.list_breakpoints": lambda _a: next(answers),
            }
        )
    )


def test_list_breakpoints_matches_the_installed_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    debugger = _list_breakpoints(_listing(("1", BREAK_AT, _RIP), _VFS_WRITE), _listing(_VFS_WRITE))
    observed = _run("debug.list_breakpoints", debugger, monkeypatch)
    assert observed == {"counts": [2, 1], "numbers_match": True, "addr_within_kallsyms": True}
    assert ("debug.clear_breakpoint", {"session_id": "ses-1", "number": "1"}) in debugger.calls


def test_list_breakpoints_needs_the_address_inside_the_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = ("1", BREAK_AT, "0xffffffff81200400")
    debugger = _list_breakpoints(_listing(outside, _VFS_WRITE), _listing(_VFS_WRITE))
    with pytest.raises(AssertionError, match="not in vfs_read"):
        _run("debug.list_breakpoints", debugger, monkeypatch)
    assert debugger.calls[-1][0] == "debug.end_session"


@pytest.mark.parametrize(
    "row",
    [
        {"number": "1", "type": "hw watchpoint", "func": BREAK_AT, "addr": _RIP},
        {"number": "1", "type": "breakpoint", "func": "ksys_read", "addr": _RIP},
    ],
    ids=["kind", "function"],
)
def test_list_breakpoints_needs_the_installed_kind_and_function(
    row: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    second = {"number": "2", "type": "breakpoint", "func": "vfs_write", "addr": _VFS_WRITE[2]}
    wrong = _ok("listed", count=2, breakpoints=[row, second])({})
    debugger = _list_breakpoints(wrong, _listing(_VFS_WRITE))
    with pytest.raises(AssertionError, match="breakpoint 1 is"):
        _run("debug.list_breakpoints", debugger, monkeypatch)


def test_list_breakpoints_needs_the_cleared_row_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    first = ("1", BREAK_AT, _RIP)
    debugger = _list_breakpoints(_listing(first, _VFS_WRITE), _listing(first))
    with pytest.raises(AssertionError, match="listed"):
        _run("debug.list_breakpoints", debugger, monkeypatch)


def test_read_frame_known_gap_fails_after_the_observable_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = _ok("read", level=0, frame={"func": BREAK_AT, "addr": _RIP})
    debugger = _Debugger(_stop_answers(**{"debug.read_frame": frame}))
    with pytest.raises(AssertionError, match="no arguments or locals"):
        _run("debug.read_frame", debugger, monkeypatch)
    assert debugger.calls[-1][0] == "debug.end_session"


def test_load_module_symbols_resolves_a_loop_function(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded = {"done": False}

    def resolve(_args: Mapping[str, object]) -> ToolResponse:
        if not loaded["done"]:
            return ToolResponse.failure("ses-1", ErrorCategory.SYMBOL_NOT_FOUND, detail="x")
        return _ok("resolved", address="0xffffffffc0001200")(_args)

    def load(args: Mapping[str, object]) -> ToolResponse:
        loaded["done"] = True
        assert args["expected_base"] == 0xFFFFFFFFC0001000
        return _ok("loaded", base_address="0xffffffffc0001000", symbols_loaded=True)(args)

    answers: dict[str, _Answer] = {
        "debug.resolve_symbol": resolve,
        "debug.load_module_symbols": load,
    }
    observed = _run("debug.load_module_symbols", _Debugger(answers), monkeypatch)
    assert observed["resolves_to_kallsyms"] is True
    early = {**answers, "debug.resolve_symbol": _ok("resolved", address="0xffffffffc0001200")}
    with pytest.raises(AssertionError, match="before its module"):
        _run("debug.load_module_symbols", _Debugger(early), monkeypatch)


def test_read_memory_must_equal_the_guest_banner(monkeypatch: pytest.MonkeyPatch) -> None:
    banner = _truth().version
    good = {"debug.read_memory": _ok("read", memory_hex=banner.hex())}
    assert _run("debug.read_memory", _Debugger(good), monkeypatch)["matches"] is True
    bad = {"debug.read_memory": _ok("read", memory_hex=(b"X" + banner[1:]).hex())}
    with pytest.raises(AssertionError, match="differs from /proc/version"):
        _run("debug.read_memory", _Debugger(bad), monkeypatch)


def test_an_admitted_second_attach_is_still_ended(monkeypatch: pytest.MonkeyPatch) -> None:
    def session(_args: Mapping[str, object]) -> ToolResponse:
        data = {"transport": "gdbstub", "run_id": "run-1", "system_id": "sys-1"}
        return ToolResponse.success("ses-1", "live", data=cast(Any, data))

    debugger = _Debugger(_stop_answers(**{"debug.get_session": session}))
    with pytest.raises(AssertionError, match="second gdbstub attach"):
        _run("debug.start_session", debugger, monkeypatch)
    ended = {args["session_id"] for tool, args in debugger.calls if tool == "debug.end_session"}
    assert ended == {"ses-1", "ses-2"}


def test_set_watchpoint_must_watch_the_kallsyms_address(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter((10, 11))

    def memory(_args: Mapping[str, object]) -> ToolResponse:
        return _ok("read", memory_hex=next(ticks).to_bytes(8, "little").hex())(_args)

    def answers(expr: str) -> dict[str, _Answer]:
        return {
            "debug.read_memory": memory,
            "debug.set_watchpoint": _ok("watching", number="2", expr=expr),
            "debug.continue": _stopped("watchpoint-trigger"),
            "debug.clear_watchpoint": _ok("cleared"),
        }

    good = answers("*(char(*)[8])0xffffffff82100000")
    assert _run("debug.set_watchpoint", _Debugger(good), monkeypatch)["value_increased"] is True
    ticks = iter((10, 11))
    with pytest.raises(AssertionError, match="not on jiffies_64"):
        _run("debug.set_watchpoint", _Debugger(answers("*(char(*)[8])0x1000")), monkeypatch)
