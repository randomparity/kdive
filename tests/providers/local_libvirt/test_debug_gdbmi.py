"""gdb-MI engine tests — every op driven against a scripted fake `MiController` (no gdb).

The engine surface ported for issue #21 (breakpoints, read_registers, read_memory cap +
bytes-verbatim, continue/interrupt) is exercised directly; the real `PygdbmiController` and
the `attach()` subprocess path are `live_vm`-gated and not unit-tested here.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.tools.debug.session_registry import GdbMiSessionRegistry
from kdive.providers.ports.debug import (
    GdbFrame,
    GdbMiAttachment,
    GdbStopRecord,
)
from kdive.providers.shared.debug_common import gdbmi
from kdive.providers.shared.debug_common.debuginfo import DebuginfoResolver
from kdive.providers.shared.debug_common.execution import ExecutionControl
from kdive.providers.shared.debug_common.gdbmi import (
    MAX_MEMORY_READ_BYTES,
    GdbMiEngine,
    MiRecord,
    PygdbmiController,
    parse_mi_records,
)
from kdive.providers.shared.debug_common.mi_protocol import (
    disassembly_rows,
    evaluate_value,
    stack_frames,
)
from kdive.providers.shared.debug_common.transcript import append_transcript
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry


class _FakeMiController:
    """Maps each MI command to a canned list of pygdbmi record dicts; scripts async reads."""

    def __init__(
        self,
        *,
        responses: dict[str, list[dict[str, object]]] | None = None,
        reads: list[list[dict[str, object]]] | None = None,
        response_timeout: bool = False,
    ) -> None:
        self._responses = responses or {}
        self._reads = list(reads or [])
        self._response_timeout = response_timeout
        self.written: list[str] = []
        self.write_timeouts: list[float] = []
        self.read_timeouts: list[float] = []
        self.exited = False

    def write(self, command: str, *, timeout_sec: float) -> list[dict[str, object]]:
        self.write_timeouts.append(timeout_sec)
        self.written.append(command)
        return self._responses.get(
            command, [{"type": "result", "message": "done", "payload": None}]
        )

    def read(self, *, timeout_sec: float) -> list[dict[str, object]]:
        self.read_timeouts.append(timeout_sec)
        return self._reads.pop(0) if self._reads else []

    def get_gdb_response(
        self, *, timeout_sec: float, raise_error_on_timeout: bool = True
    ) -> list[dict[str, object]]:
        if self._response_timeout and raise_error_on_timeout:
            raise gdbmi._timeout_error("get_gdb_response", timeout_sec)
        return []

    def exit(self) -> None:
        self.exited = True


class _TimeoutGdbController:
    def get_gdb_response(
        self, *, timeout_sec: float, raise_error_on_timeout: bool = True
    ) -> list[dict[str, object]]:
        from pygdbmi.constants import GdbTimeoutError

        del timeout_sec, raise_error_on_timeout
        raise GdbTimeoutError("timed out")

    def exit(self) -> None:
        pass


def _pygdbmi_controller(controller: object) -> PygdbmiController:
    wrapped = object.__new__(PygdbmiController)
    wrapped._controller = controller
    return wrapped


def _attachment(controller: _FakeMiController, tmp_path: Path) -> GdbMiAttachment:
    return GdbMiAttachment(
        controller=controller,
        rsp_host="127.0.0.1",
        rsp_port=1234,
        transcript_path=tmp_path / "transcript.jsonl",
    )


def _engine(redactor: Redactor | None = None) -> GdbMiEngine:
    return GdbMiEngine(redactor=redactor or Redactor(registry=SecretRegistry()))


class _ExecutionEngine:
    """Minimal engine fake for ExecutionControl's direct helper behavior."""

    def __init__(self) -> None:
        self.executed: list[str] = []
        self.transcript_commands: list[str] = []

    def records_from(self, raw: list[dict[str, object]]) -> list[MiRecord]:
        return [MiRecord.from_raw(record) for record in raw]

    def append_transcript(
        self, transcript_path: Path, command: str, records: list[MiRecord]
    ) -> None:
        del transcript_path, records
        self.transcript_commands.append(command)

    def execute_mi_command(self, attachment: GdbMiAttachment, command: str) -> list[MiRecord]:
        del attachment
        self.executed.append(command)
        return [MiRecord(type="result", message="running")]

    def stop_record_from(self, record: MiRecord) -> GdbStopRecord:
        payload = record.payload if isinstance(record.payload, dict) else {}
        reason = payload.get("reason")
        bkptno = payload.get("bkptno")
        return GdbStopRecord(
            reason=reason if isinstance(reason, str) else None,
            bkptno=bkptno if isinstance(bkptno, str) else None,
        )

    def redact_stop(self, stop: GdbStopRecord) -> GdbStopRecord:
        return stop


# --- parsing -------------------------------------------------------------------------------


def test_parse_mi_records_skips_blank_and_prompt() -> None:
    records = parse_mi_records("\n(gdb)\n^done\n")
    assert [r.type for r in records] == ["result"]


def test_mi_record_from_raw_whitelists_keys() -> None:
    record = MiRecord.from_raw({"type": "result", "message": "done", "extra": "dropped"})
    assert record.type == "result"
    assert record.message == "done"


def test_pygdbmi_response_timeout_raises_by_default() -> None:
    controller = _pygdbmi_controller(_TimeoutGdbController())

    with pytest.raises(CategorizedError) as exc:
        controller.get_gdb_response(timeout_sec=0.25)

    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert exc.value.details["code"] == "transport_stall"


def test_pygdbmi_response_timeout_can_return_empty_when_requested() -> None:
    controller = _pygdbmi_controller(_TimeoutGdbController())

    assert controller.get_gdb_response(timeout_sec=0.25, raise_error_on_timeout=False) == []


# --- breakpoints ---------------------------------------------------------------------------


def test_set_breakpoint_uses_software_insert_and_parses_ref(tmp_path: Path) -> None:
    # Software breakpoint (no -h): QEMU's gdbstub honors software breakpoints reliably, whereas
    # hardware breakpoints over the stub can silently never fire (#711).
    controller = _FakeMiController(
        responses={
            "-break-insert panic": [
                {
                    "type": "result",
                    "message": "done",
                    "payload": {"bkpt": {"number": "1", "func": "panic"}},
                }
            ]
        }
    )
    ref = _engine().set_breakpoint(_attachment(controller, tmp_path), "panic")
    assert ref.number == "1"
    assert ref.func == "panic"
    assert "-break-insert panic" in controller.written
    assert "-break-insert -h panic" not in controller.written


def test_set_breakpoint_rejects_non_identifier(tmp_path: Path) -> None:
    controller = _FakeMiController()
    with pytest.raises(CategorizedError) as exc:
        _engine().set_breakpoint(_attachment(controller, tmp_path), "panic; rm -rf /")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details == {"code": "bad_location", "location": "panic; rm -rf /"}
    assert (
        str(exc.value) == "breakpoint location must be a bare C identifier, got 'panic; rm -rf /'"
    )
    assert controller.written == []


def test_clear_breakpoint_requires_numeric_id(tmp_path: Path) -> None:
    controller = _FakeMiController()
    with pytest.raises(CategorizedError) as exc:
        _engine().clear_breakpoint(_attachment(controller, tmp_path), "abc")
    assert exc.value.details == {"code": "bad_breakpoint_id", "number": "abc"}
    assert str(exc.value) == "breakpoint id must be numeric, got 'abc'"
    assert controller.written == []


def test_clear_breakpoint_deletes(tmp_path: Path) -> None:
    controller = _FakeMiController()
    _engine().clear_breakpoint(_attachment(controller, tmp_path), "3")
    assert "-break-delete 3" in controller.written


def test_list_breakpoints_parses_table_body(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-break-list": [
                {
                    "type": "result",
                    "message": "done",
                    "payload": {
                        "BreakpointTable": {
                            "body": [
                                {
                                    "bkpt": {
                                        "number": "1",
                                        "type": "hw breakpoint",
                                        "addr": "0xffffffff81000000",
                                        "func": "panic",
                                        "what": "in panic",
                                    }
                                },
                                {"bkpt": {"number": "2", "func": "oops"}},
                            ]
                        }
                    },
                }
            ]
        }
    )
    refs = _engine().list_breakpoints(_attachment(controller, tmp_path))
    assert [r.number for r in refs] == ["1", "2"]
    first = refs[0]
    assert first.type == "hw breakpoint"
    assert first.addr == "0xffffffff81000000"
    assert first.func == "panic"
    assert first.what == "in panic"


# --- registers -----------------------------------------------------------------------------


def _register_controller() -> _FakeMiController:
    return _FakeMiController(
        responses={
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
                            {"number": "1", "value": "0xbeef"},
                            {"number": "2", "value": "0xcafe"},
                        ]
                    },
                }
            ],
        }
    )


def test_read_registers_maps_names_to_values(tmp_path: Path) -> None:
    result = _engine().read_registers(_attachment(_register_controller(), tmp_path), ["rax", "rcx"])
    assert result == {"rax": "0xdead", "rcx": "0xcafe"}


def test_read_registers_rejects_empty_list(tmp_path: Path) -> None:
    with pytest.raises(CategorizedError) as exc:
        _engine().read_registers(_attachment(_FakeMiController(), tmp_path), [])
    assert exc.value.details["code"] == "bad_register"
    assert str(exc.value) == "registers must be a non-empty list"


def test_read_registers_rejects_bad_name(tmp_path: Path) -> None:
    with pytest.raises(CategorizedError) as exc:
        _engine().read_registers(_attachment(_FakeMiController(), tmp_path), ["rax; drop"])
    assert exc.value.details["code"] == "bad_register"
    assert str(exc.value) == "invalid register name 'rax; drop'"


def test_read_registers_rejects_empty_gdb_payload(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-data-list-register-names": [
                {"type": "result", "message": "done", "payload": {"register-names": ["rax"]}}
            ],
            "-data-list-register-values x": [
                {"type": "result", "message": "done", "payload": {"register-values": []}}
            ],
        }
    )
    with pytest.raises(CategorizedError) as exc:
        _engine().read_registers(_attachment(controller, tmp_path), ["rax"])
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details == {
        "code": "missing_registers",
        "requested": ["rax"],
        "missing": ["rax"],
    }
    assert str(exc.value) == "gdb/MI omitted requested register data"


def test_read_registers_rejects_partial_gdb_payload(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
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
    with pytest.raises(CategorizedError) as exc:
        _engine().read_registers(_attachment(controller, tmp_path), ["rax", "rbx", "rcx"])
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details["code"] == "missing_registers"
    assert exc.value.details["requested"] == ["rax", "rbx", "rcx"]
    assert exc.value.details["missing"] == ["rbx"]


# --- read_memory: cap + bytes verbatim -----------------------------------------------------


def _memory_controller(address: int, byte_count: int, hex_contents: str) -> _FakeMiController:
    return _FakeMiController(
        responses={
            f"-data-read-memory-bytes 0x{address:x} {byte_count}": [
                {
                    "type": "result",
                    "message": "done",
                    "payload": {"memory": [{"contents": hex_contents}]},
                }
            ]
        }
    )


def test_read_memory_returns_concatenated_bytes(tmp_path: Path) -> None:
    controller = _memory_controller(0x1000, 4, "deadbeef")
    blob = _engine().read_memory(_attachment(controller, tmp_path), address=0x1000, byte_count=4)
    assert blob == bytes.fromhex("deadbeef")


def test_read_memory_accepts_exactly_4096(tmp_path: Path) -> None:
    payload = "ab" * MAX_MEMORY_READ_BYTES
    controller = _memory_controller(0x2000, MAX_MEMORY_READ_BYTES, payload)
    blob = _engine().read_memory(
        _attachment(controller, tmp_path), address=0x2000, byte_count=MAX_MEMORY_READ_BYTES
    )
    assert len(blob) == MAX_MEMORY_READ_BYTES


def test_read_memory_rejects_over_4096_without_command(tmp_path: Path) -> None:
    controller = _FakeMiController()
    with pytest.raises(CategorizedError) as exc:
        _engine().read_memory(
            _attachment(controller, tmp_path), address=0x3000, byte_count=MAX_MEMORY_READ_BYTES + 1
        )
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details == {
        "code": "bad_read_range",
        "byte_count": MAX_MEMORY_READ_BYTES + 1,
    }
    assert str(exc.value) == f"byte_count must be between 1 and {MAX_MEMORY_READ_BYTES}"
    assert controller.written == []  # no MI command was issued


def test_read_memory_rejects_non_int_address(tmp_path: Path) -> None:
    with pytest.raises(CategorizedError) as exc:
        _engine().read_memory(
            _attachment(_FakeMiController(), tmp_path),
            address="0x10",  # ty: ignore[invalid-argument-type]
            byte_count=4,
        )
    assert exc.value.details["code"] == "bad_read_range"
    assert str(exc.value) == "address and byte_count must be integers"


def test_read_memory_rejects_non_int_byte_count(tmp_path: Path) -> None:
    with pytest.raises(CategorizedError) as exc:
        _engine().read_memory(
            _attachment(_FakeMiController(), tmp_path),
            address=0x10,
            byte_count="4",  # ty: ignore[invalid-argument-type]
        )
    assert exc.value.details["code"] == "bad_read_range"
    assert str(exc.value) == "address and byte_count must be integers"


def test_read_memory_concatenates_multiple_segments_without_separator(
    tmp_path: Path,
) -> None:
    controller = _FakeMiController(
        responses={
            "-data-read-memory-bytes 0x4000 4": [
                {
                    "type": "result",
                    "message": "done",
                    "payload": {"memory": [{"contents": "dead"}, {"contents": "beef"}]},
                }
            ]
        }
    )
    blob = _engine().read_memory(_attachment(controller, tmp_path), address=0x4000, byte_count=4)
    assert blob == bytes.fromhex("deadbeef")


def test_read_memory_treats_segment_without_contents_as_empty(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-data-read-memory-bytes 0x4100 2": [
                {
                    "type": "result",
                    "message": "done",
                    "payload": {"memory": [{"contents": "abcd"}, {"addr": "0x4102"}]},
                }
            ]
        }
    )
    blob = _engine().read_memory(_attachment(controller, tmp_path), address=0x4100, byte_count=2)
    assert blob == bytes.fromhex("abcd")


def test_read_memory_accepts_address_zero(tmp_path: Path) -> None:
    controller = _memory_controller(0x0, 1, "ff")
    blob = _engine().read_memory(_attachment(controller, tmp_path), address=0x0, byte_count=1)
    assert blob == b"\xff"


def test_read_memory_accepts_single_byte(tmp_path: Path) -> None:
    controller = _memory_controller(0x10, 1, "aa")
    blob = _engine().read_memory(_attachment(controller, tmp_path), address=0x10, byte_count=1)
    assert blob == b"\xaa"


def test_read_memory_rejects_zero_byte_count(tmp_path: Path) -> None:
    with pytest.raises(CategorizedError) as exc:
        _engine().read_memory(
            _attachment(_FakeMiController(), tmp_path), address=0x10, byte_count=0
        )
    assert exc.value.details == {"code": "bad_read_range", "byte_count": 0}
    assert str(exc.value) == f"byte_count must be between 1 and {MAX_MEMORY_READ_BYTES}"


def test_read_memory_rejects_non_hex_contents(tmp_path: Path) -> None:
    controller = _memory_controller(0x6000, 4, "nothex!!")
    with pytest.raises(CategorizedError) as exc:
        _engine().read_memory(_attachment(controller, tmp_path), address=0x6000, byte_count=4)
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details["code"] == "bad_memory_contents"
    assert str(exc.value) == "gdb/MI returned non-hex memory contents"


def test_read_memory_rejects_short_contents(tmp_path: Path) -> None:
    controller = _memory_controller(0x6000, 4, "dead")
    with pytest.raises(CategorizedError) as exc:
        _engine().read_memory(_attachment(controller, tmp_path), address=0x6000, byte_count=4)
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details == {
        "code": "short_memory_read",
        "address": 0x6000,
        "requested": 4,
        "actual": 2,
    }
    assert str(exc.value) == "gdb/MI returned fewer memory bytes than requested"


def test_read_memory_rejects_missing_memory_payload(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-data-read-memory-bytes 0x6000 4": [
                {"type": "result", "message": "done", "payload": {}}
            ]
        }
    )
    with pytest.raises(CategorizedError) as exc:
        _engine().read_memory(_attachment(controller, tmp_path), address=0x6000, byte_count=4)
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details["code"] == "short_memory_read"
    assert exc.value.details["requested"] == 4
    assert exc.value.details["actual"] == 0


def test_read_memory_rejects_out_of_range_address(tmp_path: Path) -> None:
    with pytest.raises(CategorizedError) as exc:
        _engine().read_memory(
            _attachment(_FakeMiController(), tmp_path),
            address=0x1_0000_0000_0000_0000,
            byte_count=4,
        )
    assert exc.value.details == {
        "code": "bad_read_range",
        "address": 0x1_0000_0000_0000_0000,
    }
    assert str(exc.value) == "address out of range"


def test_read_memory_accepts_max_address(tmp_path: Path) -> None:
    controller = _memory_controller(0xFFFFFFFFFFFFFFFF, 1, "5a")
    blob = _engine().read_memory(
        _attachment(controller, tmp_path), address=0xFFFFFFFFFFFFFFFF, byte_count=1
    )
    assert blob == b"\x5a"


def test_read_memory_bytes_are_verbatim_not_redacted(tmp_path: Path) -> None:
    secret = "supersecrettoken"  # pragma: allowlist secret - fake test value
    secret_hex = secret.encode().hex()
    byte_count = len(secret)
    controller = _memory_controller(0x4000, byte_count, secret_hex)
    engine = _engine(Redactor(secret_values=[secret], registry=SecretRegistry()))
    blob = engine.read_memory(
        _attachment(controller, tmp_path), address=0x4000, byte_count=byte_count
    )
    assert blob == secret.encode()  # bytes returned verbatim, NOT masked


def test_read_memory_transcript_line_is_redacted(tmp_path: Path) -> None:
    secret = "transcriptsecret"  # pragma: allowlist secret - fake test value
    attachment = _attachment(_memory_controller(0x5000, 4, "00112233"), tmp_path)
    engine = _engine(Redactor(secret_values=[secret], registry=SecretRegistry()))
    engine.append_transcript(
        attachment.transcript_path,
        "-break-insert panic",
        [MiRecord(type="console", payload=f"loaded {secret}")],
    )
    transcript = attachment.transcript_path.read_text(encoding="utf-8")
    assert secret not in transcript
    assert "[REDACTED]" in transcript


def test_transcript_redactor_sees_secrets_registered_after_engine_creation(
    tmp_path: Path,
) -> None:
    secret = "lateprocesssecret"  # pragma: allowlist secret - fake test value
    scope = object()
    registry = SecretRegistry()
    engine = GdbMiEngine(redactor_factory=lambda: Redactor(registry=registry))
    attachment = _attachment(_memory_controller(0x5000, 4, "00112233"), tmp_path)
    registry.register(secret, scope=scope)
    try:
        engine.append_transcript(
            attachment.transcript_path,
            "-break-insert panic",
            [MiRecord(type="console", payload=f"loaded {secret}")],
        )
    finally:
        registry.release(scope)

    transcript = attachment.transcript_path.read_text(encoding="utf-8")
    assert secret not in transcript
    assert "[REDACTED]" in transcript


def test_append_transcript_creates_parent_and_redacts_jsonl(tmp_path: Path) -> None:
    secret = "helpertranscriptsecret"  # pragma: allowlist secret - fake test value
    transcript_path = tmp_path / "nested" / "debug" / "transcript.jsonl"

    append_transcript(
        transcript_path=transcript_path,
        command="<read>",
        records=[MiRecord(type="console", payload=f"loaded {secret}")],
        redactor=Redactor(secret_values=[secret], registry=SecretRegistry()),
    )

    line = transcript_path.read_text(encoding="utf-8").strip()
    entry = json.loads(line)
    assert entry["command"] == "<read>"
    assert secret not in line
    assert "[REDACTED]" in line
    # The entry carries the canonical keys, and the timestamp is UTC-aware (offset present).
    assert set(entry) == {"observed_at", "command", "records"}
    assert isinstance(entry["records"], list)
    assert entry["records"][0]["type"] == "console"
    observed = datetime.fromisoformat(entry["observed_at"])
    assert observed.tzinfo is not None
    assert observed.utcoffset() == timedelta(0)


# --- continue / interrupt ------------------------------------------------------------------


@pytest.mark.parametrize("timeout_sec", [-1.0, math.inf, math.nan])
def test_execution_control_rejects_bad_timeout_before_resume(
    timeout_sec: float, tmp_path: Path
) -> None:
    engine = _ExecutionEngine()
    control = ExecutionControl(engine, command_timeout_sec=1.0)

    with pytest.raises(CategorizedError) as exc:
        control.resume(
            _attachment(_FakeMiController(), tmp_path),
            "-exec-continue",
            timeout_sec=timeout_sec,
        )

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["code"] == "bad_continue_timeout"
    reported = exc.value.details["timeout_sec"]
    assert isinstance(reported, float)
    assert reported == timeout_sec or (math.isnan(reported) and math.isnan(timeout_sec))
    assert str(exc.value) == "gdb/MI continue timeout must be a finite non-negative number"
    assert engine.executed == []


def test_execution_control_wait_for_stop_floor_is_one_slice(
    tmp_path: Path,
) -> None:
    engine = _ExecutionEngine()
    control = ExecutionControl(engine, command_timeout_sec=1.0)
    controller = _FakeMiController(reads=[])
    attachment = _attachment(controller, tmp_path)

    stop = control.wait_for_stop(attachment, timeout_sec=0.0)

    assert stop is None
    # max(1, int(0.0 / 0.5) + 1) == max(1, 1) == 1 -> exactly one poll slice at the slice timeout.
    # This case pins the max() floor: a floor-of-2 mutant (max(2, ...)) would poll twice here.
    assert controller.read_timeouts == [0.5]


def test_execution_control_wait_for_stop_counts_slices_for_positive_timeout(
    tmp_path: Path,
) -> None:
    engine = _ExecutionEngine()
    control = ExecutionControl(engine, command_timeout_sec=1.0)
    controller = _FakeMiController(reads=[])
    attachment = _attachment(controller, tmp_path)

    stop = control.wait_for_stop(attachment, timeout_sec=1.0)

    assert stop is None
    # max(1, int(1.0 / 0.5) + 1) == max(1, 3) == 3 poll slices. A positive timeout is required to
    # pin the +1 term: at timeout=0.0 both the real arithmetic and the +1-dropped mutant collapse to
    # max(1, 0(+1)) == 1. At 1.0 the dropped-+1 mutant yields 2 slices and the +2 mutant 4, so the
    # exact 3-slice count distinguishes the off-by-one slice arithmetic in either direction.
    assert controller.read_timeouts == [0.5, 0.5, 0.5]


def test_execution_control_wait_for_stop_records_reads_and_transcript(
    tmp_path: Path,
) -> None:
    engine = _ExecutionEngine()
    control = ExecutionControl(engine, command_timeout_sec=1.0)
    attachment = _attachment(
        _FakeMiController(
            reads=[
                [{"type": "notify", "message": "running", "payload": None}],
                [
                    {
                        "type": "notify",
                        "message": "stopped",
                        "payload": {"reason": "breakpoint-hit", "bkptno": "1"},
                    }
                ],
            ]
        ),
        tmp_path,
    )

    stop = control.wait_for_stop(attachment, timeout_sec=1.0)

    assert stop is not None
    assert stop.reason == "breakpoint-hit"
    assert stop.bkptno == "1"
    messages = [record.message for record in attachment.records if isinstance(record, MiRecord)]
    assert messages == ["running", "stopped"]
    assert engine.transcript_commands == ["<read>", "<read>"]


def test_execution_control_resume_raises_transport_stall_after_interrupt_timeout(
    tmp_path: Path,
) -> None:
    engine = _ExecutionEngine()
    control = ExecutionControl(engine, command_timeout_sec=1.0)
    controller = _FakeMiController(
        responses={"-exec-interrupt": [{"type": "result", "message": "done"}]},
    )
    attachment = _attachment(
        controller,
        tmp_path,
    )

    with pytest.raises(CategorizedError) as exc:
        control.resume(attachment, "-exec-continue", timeout_sec=1.0)

    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert exc.value.details["code"] == "transport_stall"
    assert exc.value.details["verb"] == "-exec-continue"
    assert (
        str(exc.value)
        == "gdb/MI RSP went silent: interrupt issued but no *stopped arrived; link stalled"
    )
    assert engine.executed == ["-exec-continue"]
    assert controller.written == ["-exec-interrupt"]
    # interrupt issues its write at the configured command timeout and transcribes the verb.
    assert controller.write_timeouts == [1.0]
    assert "-exec-interrupt" in engine.transcript_commands


class _CaptureStopEngine(_ExecutionEngine):
    """An engine whose continue command itself returns an early ``*stopped`` (#711)."""

    def execute_mi_command(self, attachment: GdbMiAttachment, command: str) -> list[MiRecord]:
        del attachment
        self.executed.append(command)
        return [
            MiRecord(type="result", message="running"),
            MiRecord(
                type="notify",
                message="stopped",
                payload={"reason": "breakpoint-hit", "bkptno": "1"},
            ),
        ]


def test_resume_returns_stop_captured_by_continue_command(tmp_path: Path) -> None:
    # Test A (#711): the breakpoint fires within milliseconds, so the continue command's own
    # reader captures ^running AND the *stopped. resume() must surface that stop without polling
    # the stream afresh (where the already-consumed *stopped would be gone -> false stall).
    engine = _CaptureStopEngine()
    control = ExecutionControl(engine, command_timeout_sec=1.0)
    controller = _FakeMiController(reads=[])
    attachment = _attachment(controller, tmp_path)

    stop = control.resume(attachment, "-exec-continue", timeout_sec=1.0)

    assert stop.reason == "breakpoint-hit"
    assert stop.bkptno == "1"
    assert engine.executed == ["-exec-continue"]
    # No fresh stream poll and no interrupt: the stop came from the continue records.
    assert controller.read_timeouts == []
    assert controller.written == []


def test_resume_falls_through_to_wait_when_continue_has_no_stop(tmp_path: Path) -> None:
    # Test B (#711 regression guard): a slow breakpoint leaves the continue command with only
    # ^running, so resume() must still poll wait_for_stop for the later *stopped.
    engine = _ExecutionEngine()
    control = ExecutionControl(engine, command_timeout_sec=1.0)
    controller = _FakeMiController(
        reads=[
            [{"type": "notify", "message": "stopped", "payload": {"reason": "breakpoint-hit"}}],
        ],
    )
    attachment = _attachment(controller, tmp_path)

    stop = control.resume(attachment, "-exec-continue", timeout_sec=1.0)

    assert stop.reason == "breakpoint-hit"
    assert engine.executed == ["-exec-continue"]
    # The fall-through poll ran (at least one read slice) and no interrupt was needed.
    assert controller.read_timeouts == [0.5]
    assert controller.written == []


def test_continue_returns_stop_on_breakpoint_hit(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={"-exec-continue": [{"type": "result", "message": "running", "payload": None}]},
        reads=[
            [
                {
                    "type": "notify",
                    "message": "stopped",
                    "payload": {"reason": "breakpoint-hit", "bkptno": "1"},
                }
            ]
        ],
    )
    stop = _engine().continue_(_attachment(controller, tmp_path), timeout_sec=1)
    assert stop.reason == "breakpoint-hit"
    assert stop.bkptno == "1"
    assert stop.timed_out is False


def test_continue_interrupts_on_timeout(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-exec-continue": [{"type": "result", "message": "running", "payload": None}],
            "-exec-interrupt": [{"type": "result", "message": "done", "payload": None}],
        },
        # The resume wait (3 slices for timeout_sec=1) yields nothing; the post-interrupt wait
        # then yields the SIGINT stop.
        reads=[
            [],
            [],
            [],
            [{"type": "notify", "message": "stopped", "payload": {"reason": "signal-received"}}],
        ],
    )
    stop = _engine().continue_(_attachment(controller, tmp_path), timeout_sec=1)
    assert stop.timed_out is True
    assert "-exec-interrupt" in controller.written


def test_continue_zero_timeout_uses_interactive_wait_cap(tmp_path: Path) -> None:
    resume_reads = int(gdbmi.MAX_INTERACTIVE_WAIT_SEC / gdbmi._STOP_POLL_SLICE_SEC) + 1
    controller = _FakeMiController(
        responses={
            "-exec-continue": [{"type": "result", "message": "running", "payload": None}],
            "-exec-interrupt": [{"type": "result", "message": "done", "payload": None}],
        },
        reads=[
            *([] for _ in range(resume_reads)),
            [{"type": "notify", "message": "stopped", "payload": {"reason": "signal-received"}}],
        ],
    )

    stop = _engine().continue_(_attachment(controller, tmp_path), timeout_sec=0.0)

    assert stop.timed_out is True
    assert controller.written == ["-exec-continue", "-exec-interrupt"]
    assert len(controller.read_timeouts) == resume_reads + 1


@pytest.mark.parametrize("timeout_sec", [-1.0, math.inf, math.nan])
def test_continue_rejects_invalid_timeout(timeout_sec: float, tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={"-exec-continue": [{"type": "result", "message": "running", "payload": None}]},
    )

    with pytest.raises(CategorizedError) as exc:
        _engine().continue_(_attachment(controller, tmp_path), timeout_sec=timeout_sec)

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["code"] == "bad_continue_timeout"
    assert controller.written == []


def test_continue_raises_transport_stall_on_silent_link(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-exec-continue": [{"type": "result", "message": "running", "payload": None}],
            "-exec-interrupt": [{"type": "result", "message": "done", "payload": None}],
        },
        reads=[],  # never any stop, even after interrupt -> silent link
    )
    with pytest.raises(CategorizedError) as exc:
        _engine().continue_(_attachment(controller, tmp_path), timeout_sec=1)
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert exc.value.details["code"] == "transport_stall"


def test_interrupt_returns_stop(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={"-exec-interrupt": [{"type": "result", "message": "done", "payload": None}]},
        reads=[
            [{"type": "notify", "message": "stopped", "payload": {"reason": "signal-received"}}]
        ],
    )
    controller_attachment = _attachment(controller, tmp_path)
    stop = _engine().interrupt(controller_attachment)
    assert stop is not None
    assert stop.reason == "signal-received"
    # interrupt drives its write through ExecutionControl at the engine's command timeout.
    assert controller.write_timeouts == [10.0]


def test_interrupt_returns_none_when_no_stop(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={"-exec-interrupt": [{"type": "result", "message": "done", "payload": None}]},
        reads=[],
    )
    assert _engine().interrupt(_attachment(controller, tmp_path)) is None


# --- evaluate_value helper -----------------------------------------------------------------


def test_evaluate_value_returns_value_string() -> None:
    records = [MiRecord(type="result", message="done", payload={"value": "(int *) 0x10 <s>"})]
    assert evaluate_value(records) == "(int *) 0x10 <s>"


def test_evaluate_value_returns_none_without_value_key() -> None:
    assert evaluate_value([MiRecord(type="result", message="done", payload={})]) is None


def test_evaluate_value_returns_none_for_non_result_records() -> None:
    assert evaluate_value([MiRecord(type="notify", message="stopped")]) is None


def test_evaluate_value_returns_none_for_non_string_value() -> None:
    records = [MiRecord(type="result", message="done", payload={"value": 16})]
    assert evaluate_value(records) is None


# --- stack_frames helper -------------------------------------------------------------------


def test_stack_frames_extracts_frame_rows() -> None:
    records = [
        MiRecord(
            type="result",
            message="done",
            payload={
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
        )
    ]
    rows = stack_frames(records)
    assert [row.get("func") for row in rows] == ["panic", "do_exit"]


def test_stack_frames_extracts_bare_frame_rows() -> None:
    # Real gdb/pygdbmi emits ``stack=[frame={...},...]`` flattened to bare frame dicts
    # (no ``"frame"`` wrapper) — the shape observed in a live gdbstub transcript. The parser
    # must read these directly, not only the wrapped form.
    records = [
        MiRecord(
            type="result",
            message="done",
            payload={
                "stack": [
                    {
                        "level": "0",
                        "func": "schedule",
                        "addr": "0xffffffff82455b20",
                        "file": "kernel/sched/core.c",
                        "line": "6999",
                    },
                    {"level": "1", "func": "worker_thread", "addr": "0xffffffff81328350"},
                ]
            },
        )
    ]
    rows = stack_frames(records)
    assert [row.get("func") for row in rows] == ["schedule", "worker_thread"]


def test_stack_frames_empty_for_missing_or_non_list_stack() -> None:
    assert stack_frames([MiRecord(type="result", message="done", payload={})]) == []
    assert stack_frames([MiRecord(type="result", message="done", payload={"stack": "oops"})]) == []
    assert stack_frames([MiRecord(type="result", message="done", payload={"stack": []})]) == []


# --- disassembly_rows helper (ADR-0276) ----------------------------------------------------


def test_disassembly_rows_extracts_instruction_rows() -> None:
    records = [
        MiRecord(
            type="result",
            message="done",
            payload={
                "asm_insns": [
                    {
                        "address": "0xffffffff81000000",
                        "func-name": "panic",
                        "offset": "0",
                        "inst": "push %rbp",
                    },
                    {"address": "0xffffffff81000001", "inst": "mov %rsp,%rbp"},
                ]
            },
        )
    ]
    rows = disassembly_rows(records)
    assert [r.get("inst") for r in rows] == ["push %rbp", "mov %rsp,%rbp"]


def test_disassembly_rows_empty_for_missing_or_non_list_asm_insns() -> None:
    assert disassembly_rows([MiRecord(type="result", message="done", payload={})]) == []
    assert (
        disassembly_rows([MiRecord(type="result", message="done", payload={"asm_insns": "oops"})])
        == []
    )


# --- symbol resolution ---------------------------------------------------------------------


def test_resolve_symbol_returns_data_global_address(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-data-evaluate-expression &d_hash_shift": [
                {
                    "type": "result",
                    "message": "done",
                    "payload": {"value": "(int *) 0xffffffff82a1b3c0 <d_hash_shift>"},
                }
            ]
        }
    )
    address = _engine().resolve_symbol(_attachment(controller, tmp_path), "d_hash_shift")
    assert address == 0xFFFFFFFF82A1B3C0
    # The only expression ever sent is &<identifier> (address-of-a-name); non-injectable.
    assert "-data-evaluate-expression &d_hash_shift" in controller.written


def test_resolve_symbol_returns_function_address(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-data-evaluate-expression &panic": [
                {
                    "type": "result",
                    "message": "done",
                    "payload": {"value": "(void (*)(void)) 0xffffffff81000000 <panic>"},
                }
            ]
        }
    )
    # &name resolves a function too, unlike -break-insert (a code location only).
    address = _engine().resolve_symbol(_attachment(controller, tmp_path), "panic")
    assert address == 0xFFFFFFFF81000000


def test_resolve_symbol_accepts_zero_address(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-data-evaluate-expression &weak_sym": [
                {"type": "result", "message": "done", "payload": {"value": "0x0"}}
            ]
        }
    )
    # A weak/absent symbol resolving to 0x0 is a valid address, not an error.
    assert _engine().resolve_symbol(_attachment(controller, tmp_path), "weak_sym") == 0


def test_resolve_symbol_parses_plain_hex_without_cast(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-data-evaluate-expression &x": [
                {"type": "result", "message": "done", "payload": {"value": "0xdead"}}
            ]
        }
    )
    assert _engine().resolve_symbol(_attachment(controller, tmp_path), "x") == 0xDEAD


def test_resolve_symbol_rejects_non_identifier(tmp_path: Path) -> None:
    controller = _FakeMiController()
    with pytest.raises(CategorizedError) as exc:
        _engine().resolve_symbol(_attachment(controller, tmp_path), "d_hash_shift; rm -rf /")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details == {"code": "bad_symbol_name", "name": "d_hash_shift; rm -rf /"}
    assert str(exc.value) == "symbol name must be a bare C identifier, got 'd_hash_shift; rm -rf /'"
    assert controller.written == []  # no MI command issued for a bad name


def test_resolve_symbol_rejects_empty_name(tmp_path: Path) -> None:
    controller = _FakeMiController()
    with pytest.raises(CategorizedError) as exc:
        _engine().resolve_symbol(_attachment(controller, tmp_path), "")
    assert exc.value.details["code"] == "bad_symbol_name"
    assert controller.written == []


def test_resolve_symbol_maps_gdb_error_to_attach_failure(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-data-evaluate-expression &nope": [
                {"type": "result", "message": "error", "payload": {"msg": 'No symbol "nope"'}}
            ]
        }
    )
    # An unknown symbol surfaces as DEBUG_ATTACH_FAILURE via execute_mi_command, the same
    # contract set_breakpoint has for a bad symbol today.
    with pytest.raises(CategorizedError) as exc:
        _engine().resolve_symbol(_attachment(controller, tmp_path), "nope")
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details["command"] == "-data-evaluate-expression &nope"


def test_resolve_symbol_rejects_missing_value(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-data-evaluate-expression &s": [{"type": "result", "message": "done", "payload": {}}]
        }
    )
    with pytest.raises(CategorizedError) as exc:
        _engine().resolve_symbol(_attachment(controller, tmp_path), "s")
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details["code"] == "bad_symbol_value"
    assert exc.value.details["name"] == "s"


def test_resolve_symbol_rejects_unparseable_value_and_redacts_it(tmp_path: Path) -> None:
    secret = "supersecretvalue"  # pragma: allowlist secret - fake test value
    controller = _FakeMiController(
        responses={
            "-data-evaluate-expression &s": [
                {"type": "result", "message": "done", "payload": {"value": f"void {secret}"}}
            ]
        }
    )
    engine = _engine(Redactor(secret_values=[secret], registry=SecretRegistry()))
    with pytest.raises(CategorizedError) as exc:
        engine.resolve_symbol(_attachment(controller, tmp_path), "s")
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details["code"] == "bad_symbol_value"
    assert secret not in str(exc.value.details["value"])  # echoed value is redacted


# --- stack walking: backtrace / read_frame -------------------------------------------------


def _stack_controller(
    frames: list[dict[str, object]], command: str = "-stack-list-frames"
) -> _FakeMiController:
    # Bare frame dicts — the shape real gdb/pygdbmi emits (live-verified), not a ``{"frame": ...}``
    # wrapper. ``test_stack_frames_extracts_frame_rows`` covers the wrapped form separately.
    return _FakeMiController(
        responses={
            command: [
                {
                    "type": "result",
                    "message": "done",
                    "payload": {"stack": list(frames)},
                }
            ]
        }
    )


def test_backtrace_returns_structured_frames(tmp_path: Path) -> None:
    controller = _stack_controller(
        [
            {
                "level": "0",
                "func": "panic",
                "addr": "0xffffffff81000000",
                "file": "kernel/panic.c",
                "line": "42",
            },
            {"level": "1", "func": "do_exit", "addr": "0xffffffff81001000"},
        ]
    )
    bt = _engine().backtrace(_attachment(controller, tmp_path), max_frames=64)
    assert bt.truncated is False
    assert [frame.level for frame in bt.frames] == [0, 1]
    assert bt.frames[0].func == "panic"
    assert bt.frames[0].file == "kernel/panic.c"
    assert bt.frames[0].line == 42
    assert "-stack-list-frames" in controller.written


def test_backtrace_truncates_to_max_frames(tmp_path: Path) -> None:
    frames: list[dict[str, object]] = [{"level": str(i), "func": f"f{i}"} for i in range(5)]
    bt = _engine().backtrace(_attachment(_stack_controller(frames), tmp_path), max_frames=3)
    assert bt.truncated is True
    assert [frame.level for frame in bt.frames] == [0, 1, 2]


@pytest.mark.parametrize("bad", [0, gdbmi.MAX_BACKTRACE_FRAMES + 1])
def test_backtrace_rejects_bad_max_frames_before_command(bad: int, tmp_path: Path) -> None:
    controller = _FakeMiController()
    with pytest.raises(CategorizedError) as exc:
        _engine().backtrace(_attachment(controller, tmp_path), max_frames=bad)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["code"] == "bad_frame_count"
    assert controller.written == []


def test_backtrace_raises_no_frames_on_empty_stack(tmp_path: Path) -> None:
    with pytest.raises(CategorizedError) as exc:
        _engine().backtrace(_attachment(_stack_controller([]), tmp_path), max_frames=64)
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details["code"] == "no_frames"


def test_backtrace_raises_no_frames_on_malformed_stack(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-stack-list-frames": [
                {"type": "result", "message": "done", "payload": {"stack": "garbage"}}
            ]
        }
    )
    with pytest.raises(CategorizedError) as exc:
        _engine().backtrace(_attachment(controller, tmp_path), max_frames=64)
    assert exc.value.details["code"] == "no_frames"


def test_backtrace_classifies_running_inferior(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-stack-list-frames": [
                {
                    "type": "result",
                    "message": "error",
                    "payload": {"msg": "Cannot execute this command while the target is running."},
                }
            ]
        }
    )
    with pytest.raises(CategorizedError) as exc:
        _engine().backtrace(_attachment(controller, tmp_path), max_frames=64)
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details["code"] == "inferior_running"


def test_backtrace_classifies_no_stack_error(tmp_path: Path) -> None:
    # Real gdb answers an unwindable target with `^error,"No stack."`, not an empty `^done`.
    controller = _FakeMiController(
        responses={
            "-stack-list-frames": [
                {"type": "result", "message": "error", "payload": {"msg": "No stack."}}
            ]
        }
    )
    with pytest.raises(CategorizedError) as exc:
        _engine().backtrace(_attachment(controller, tmp_path), max_frames=64)
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details["code"] == "no_frames"


def test_backtrace_passes_through_other_gdb_errors(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-stack-list-frames": [
                {
                    "type": "result",
                    "message": "error",
                    "payload": {"msg": "Cannot access memory at address 0x0"},
                }
            ]
        }
    )
    with pytest.raises(CategorizedError) as exc:
        _engine().backtrace(_attachment(controller, tmp_path), max_frames=64)
    # An unrelated gdb error keeps the generic command-failure shape, not a stack-walk code.
    assert exc.value.details["command"] == "-stack-list-frames"
    assert exc.value.details.get("code") not in {"inferior_running", "no_frames"}


def test_backtrace_redacts_registered_secret_in_func(tmp_path: Path) -> None:
    secret = "topsecretfunc"  # pragma: allowlist secret - fake test value
    controller = _stack_controller([{"level": "0", "func": secret}])
    engine = _engine(Redactor(secret_values=[secret], registry=SecretRegistry()))
    bt = engine.backtrace(_attachment(controller, tmp_path), max_frames=64)
    assert bt.frames[0].func is not None
    assert secret not in bt.frames[0].func


def test_read_frame_returns_single_frame(tmp_path: Path) -> None:
    controller = _stack_controller(
        [{"level": "2", "func": "schedule", "addr": "0xffffffff8100a000"}],
        command="-stack-list-frames 2 2",
    )
    frame = _engine().read_frame(_attachment(controller, tmp_path), level=2)
    assert frame.level == 2
    assert frame.func == "schedule"
    assert "-stack-list-frames 2 2" in controller.written


def test_read_frame_reaches_past_backtrace_cap(tmp_path: Path) -> None:
    controller = _stack_controller(
        [{"level": "70", "func": "deep"}], command="-stack-list-frames 70 70"
    )
    frame = _engine().read_frame(_attachment(controller, tmp_path), level=70)
    assert frame.level == 70
    assert "-stack-list-frames 70 70" in controller.written


def test_read_frame_rejects_negative_level_before_command(tmp_path: Path) -> None:
    controller = _FakeMiController()
    with pytest.raises(CategorizedError) as exc:
        _engine().read_frame(_attachment(controller, tmp_path), level=-1)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["code"] == "bad_frame_level"
    assert controller.written == []


def test_read_frame_raises_no_frame_at_level(tmp_path: Path) -> None:
    controller = _stack_controller([], command="-stack-list-frames 9 9")
    with pytest.raises(CategorizedError) as exc:
        _engine().read_frame(_attachment(controller, tmp_path), level=9)
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details["code"] == "no_frame_at_level"
    assert exc.value.details["level"] == 9


def test_read_frame_classifies_out_of_range_not_enough_frames(tmp_path: Path) -> None:
    # Real gdb answers `-stack-list-frames N N` past stack depth with
    # `^error,"-stack-list-frames: Not enough frames in stack."` (live-verified over a gdbstub) —
    # not an empty `^done` and not "No frame at level N.". The out-of-range case must still map to
    # no_frame_at_level, so the classifier matches this phrasing too.
    controller = _FakeMiController(
        responses={
            "-stack-list-frames 999 999": [
                {
                    "type": "result",
                    "message": "error",
                    "payload": {"msg": "-stack-list-frames: Not enough frames in stack."},
                }
            ]
        }
    )
    with pytest.raises(CategorizedError) as exc:
        _engine().read_frame(_attachment(controller, tmp_path), level=999)
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details["code"] == "no_frame_at_level"
    assert exc.value.details["level"] == 999


def test_read_frame_classifies_no_frame_error(tmp_path: Path) -> None:
    # Real gdb answers an out-of-range level with `^error,"No frame at level N."`, not `^done`.
    controller = _FakeMiController(
        responses={
            "-stack-list-frames 9 9": [
                {
                    "type": "result",
                    "message": "error",
                    "payload": {"msg": "No frame at level 9."},
                }
            ]
        }
    )
    with pytest.raises(CategorizedError) as exc:
        _engine().read_frame(_attachment(controller, tmp_path), level=9)
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details["code"] == "no_frame_at_level"
    assert exc.value.details["level"] == 9


def test_read_frame_classifies_running_inferior(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-stack-list-frames 0 0": [
                {
                    "type": "result",
                    "message": "error",
                    "payload": {"msg": "Selected thread is running."},
                }
            ]
        }
    )
    with pytest.raises(CategorizedError) as exc:
        _engine().read_frame(_attachment(controller, tmp_path), level=0)
    assert exc.value.details["code"] == "inferior_running"


# --- disassembly (ADR-0276) ----------------------------------------------------------------


def _disasm_controller(insns: list[dict[str, object]], command: str) -> _FakeMiController:
    return _FakeMiController(
        responses={
            command: [{"type": "result", "message": "done", "payload": {"asm_insns": list(insns)}}]
        }
    )


def test_disassemble_symbol_resolves_then_disassembles(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-data-evaluate-expression &schedule": [
                {
                    "type": "result",
                    "message": "done",
                    "payload": {"value": "0xffffffff81000000 <schedule>"},
                }
            ],
            "-data-disassemble -s 0xffffffff81000000 -e 0xffffffff81000400 -- 0": [
                {
                    "type": "result",
                    "message": "done",
                    "payload": {
                        "asm_insns": [
                            {
                                "address": "0xffffffff81000000",
                                "func-name": "schedule",
                                "offset": "0",
                                "inst": "push %rbp",
                            },
                        ]
                    },
                }
            ],
        }
    )
    result = _engine().disassemble(
        _attachment(controller, tmp_path), symbol="schedule", address=None, instruction_count=64
    )
    assert result.truncated is False
    assert result.instructions[0].inst == "push %rbp"
    assert result.instructions[0].func_name == "schedule"
    assert result.instructions[0].offset == 0


def test_disassemble_address_skips_symbol_resolution(tmp_path: Path) -> None:
    command = "-data-disassemble -s 0x1000 -e 0x1400 -- 0"
    controller = _disasm_controller([{"address": "0x1000", "inst": "nop"}], command)
    result = _engine().disassemble(
        _attachment(controller, tmp_path), symbol=None, address=0x1000, instruction_count=64
    )
    assert result.instructions[0].address == "0x1000"
    assert "-data-evaluate-expression &" not in " ".join(controller.written)


def test_disassemble_truncates_to_instruction_count(tmp_path: Path) -> None:
    insns: list[dict[str, object]] = [
        {"address": f"0x{0x1000 + i:x}", "inst": "nop"} for i in range(5)
    ]
    controller = _disasm_controller(insns, "-data-disassemble -s 0x1000 -e 0x1030 -- 0")
    result = _engine().disassemble(
        _attachment(controller, tmp_path), symbol=None, address=0x1000, instruction_count=3
    )
    assert result.truncated is True
    assert len(result.instructions) == 3


@pytest.mark.parametrize("bad", [0, gdbmi.MAX_DISASSEMBLE_INSTRUCTIONS + 1])
def test_disassemble_rejects_bad_count_before_command(bad: int, tmp_path: Path) -> None:
    controller = _FakeMiController()
    with pytest.raises(CategorizedError) as exc:
        _engine().disassemble(
            _attachment(controller, tmp_path), symbol=None, address=0x1000, instruction_count=bad
        )
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["code"] == "bad_instruction_count"
    assert controller.written == []


@pytest.mark.parametrize(("symbol", "address"), [("schedule", 0x1000), (None, None)])
def test_disassemble_rejects_bad_target(
    symbol: str | None, address: int | None, tmp_path: Path
) -> None:
    controller = _FakeMiController()
    with pytest.raises(CategorizedError) as exc:
        _engine().disassemble(
            _attachment(controller, tmp_path),
            symbol=symbol,
            address=address,
            instruction_count=64,
        )
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["code"] == "bad_target"
    assert controller.written == []


@pytest.mark.parametrize("bad", [-1, 0x1_0000_0000_0000_0000])
def test_disassemble_rejects_out_of_range_address(bad: int, tmp_path: Path) -> None:
    controller = _FakeMiController()
    with pytest.raises(CategorizedError) as exc:
        _engine().disassemble(
            _attachment(controller, tmp_path), symbol=None, address=bad, instruction_count=64
        )
    assert exc.value.details["code"] == "bad_address"
    assert controller.written == []


def test_disassemble_no_instructions_on_empty_asm_insns(tmp_path: Path) -> None:
    controller = _disasm_controller([], "-data-disassemble -s 0x1000 -e 0x1400 -- 0")
    with pytest.raises(CategorizedError) as exc:
        _engine().disassemble(
            _attachment(controller, tmp_path), symbol=None, address=0x1000, instruction_count=64
        )
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details["code"] == "no_instructions"


def test_disassemble_no_instructions_on_malformed_asm_insns(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-data-disassemble -s 0x1000 -e 0x1400 -- 0": [
                {"type": "result", "message": "done", "payload": {"asm_insns": "garbage"}}
            ]
        }
    )
    with pytest.raises(CategorizedError) as exc:
        _engine().disassemble(
            _attachment(controller, tmp_path), symbol=None, address=0x1000, instruction_count=64
        )
    assert exc.value.details["code"] == "no_instructions"


def test_disassemble_shrink_retry_returns_prefix(tmp_path: Path) -> None:
    # The full N*16 window is unreadable; the halved window succeeds and still holds >N.
    controller = _FakeMiController(
        responses={
            "-data-disassemble -s 0x1000 -e 0x1400 -- 0": [
                {
                    "type": "result",
                    "message": "error",
                    "payload": {"msg": "Cannot access memory at address 0x1400"},
                }
            ],
            "-data-disassemble -s 0x1000 -e 0x1200 -- 0": [
                {
                    "type": "result",
                    "message": "done",
                    "payload": {
                        "asm_insns": [
                            {"address": f"0x{0x1000 + i:x}", "inst": "nop"} for i in range(80)
                        ]
                    },
                }
            ],
        }
    )
    result = _engine().disassemble(
        _attachment(controller, tmp_path), symbol=None, address=0x1000, instruction_count=64
    )
    assert len(result.instructions) == 64
    assert result.truncated is True


def test_disassemble_no_instructions_when_floor_window_unreadable(tmp_path: Path) -> None:
    # Every window down to the 16-byte floor errors with a memory-access message.
    controller = _FakeMiController(
        responses={
            f"-data-disassemble -s 0x1000 -e 0x{0x1000 + span:x} -- 0": [
                {
                    "type": "result",
                    "message": "error",
                    "payload": {"msg": "Cannot access memory at address 0x1000"},
                }
            ]
            for span in (1024, 512, 256, 128, 64, 32, 16)
        }
    )
    with pytest.raises(CategorizedError) as exc:
        _engine().disassemble(
            _attachment(controller, tmp_path), symbol=None, address=0x1000, instruction_count=64
        )
    assert exc.value.details["code"] == "no_instructions"


def test_disassemble_passes_through_unrelated_gdb_error(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-data-disassemble -s 0x1000 -e 0x1400 -- 0": [
                {
                    "type": "result",
                    "message": "error",
                    "payload": {"msg": "Some other gdb failure"},
                }
            ]
        }
    )
    with pytest.raises(CategorizedError) as exc:
        _engine().disassemble(
            _attachment(controller, tmp_path), symbol=None, address=0x1000, instruction_count=64
        )
    assert exc.value.details.get("code") != "no_instructions"
    assert exc.value.details["command"] == "-data-disassemble -s 0x1000 -e 0x1400 -- 0"


def test_disassemble_redacts_registered_secret_in_inst(tmp_path: Path) -> None:
    secret = "topsecretsym"  # pragma: allowlist secret - fake test value
    controller = _disasm_controller(
        [{"address": "0x1000", "inst": "call", "func-name": secret}],
        "-data-disassemble -s 0x1000 -e 0x1400 -- 0",
    )
    engine = _engine(Redactor(secret_values=[secret], registry=SecretRegistry()))
    result = engine.disassemble(
        _attachment(controller, tmp_path), symbol=None, address=0x1000, instruction_count=64
    )
    assert result.instructions[0].func_name is not None
    assert secret not in result.instructions[0].func_name


# --- watchpoints (ADR-0277) ----------------------------------------------------------------


def _watch_set_controller(command: str) -> _FakeMiController:
    return _FakeMiController(
        responses={
            command: [
                {
                    "type": "result",
                    "message": "done",
                    "payload": {"wpt": {"number": "2", "exp": "*(char(*)[8])0x1000"}},
                }
            ]
        }
    )


def test_set_watchpoint_symbol_resolves_then_watches(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-data-evaluate-expression &d_hash_shift": [
                {
                    "type": "result",
                    "message": "done",
                    "payload": {"value": "0xffffffff81000000 <d_hash_shift>"},
                }
            ],
            "-break-watch *(char(*)[8])0xffffffff81000000": [
                {
                    "type": "result",
                    "message": "done",
                    "payload": {"wpt": {"number": "3", "exp": "*(char(*)[8])0xffffffff81000000"}},
                }
            ],
        }
    )
    ref = _engine().set_watchpoint(
        _attachment(controller, tmp_path), symbol="d_hash_shift", address=None, byte_count=8
    )
    assert ref.number == "3"
    assert ref.expr == "*(char(*)[8])0xffffffff81000000"


def test_set_watchpoint_address_skips_symbol_resolution(tmp_path: Path) -> None:
    command = "-break-watch *(char(*)[4])0x1000"
    controller = _watch_set_controller(command)
    ref = _engine().set_watchpoint(
        _attachment(controller, tmp_path), symbol=None, address=0x1000, byte_count=4
    )
    assert ref.number == "2"
    assert "-data-evaluate-expression &" not in " ".join(controller.written)
    assert command in controller.written


@pytest.mark.parametrize("bad", [0, 3, 16])
def test_set_watchpoint_rejects_bad_byte_count_before_command(bad: int, tmp_path: Path) -> None:
    controller = _FakeMiController()
    with pytest.raises(CategorizedError) as exc:
        _engine().set_watchpoint(
            _attachment(controller, tmp_path), symbol=None, address=0x1000, byte_count=bad
        )
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["code"] == "bad_byte_count"
    assert exc.value.details["supported"] == [1, 2, 4, 8]
    assert controller.written == []


@pytest.mark.parametrize(("symbol", "address"), [("d_hash_shift", 0x1000), (None, None)])
def test_set_watchpoint_rejects_bad_target(
    symbol: str | None, address: int | None, tmp_path: Path
) -> None:
    controller = _FakeMiController()
    with pytest.raises(CategorizedError) as exc:
        _engine().set_watchpoint(
            _attachment(controller, tmp_path), symbol=symbol, address=address, byte_count=8
        )
    assert exc.value.details["code"] == "bad_target"
    assert controller.written == []


@pytest.mark.parametrize("bad", [-1, 0x1_0000_0000_0000_0000])
def test_set_watchpoint_rejects_out_of_range_address(bad: int, tmp_path: Path) -> None:
    controller = _FakeMiController()
    with pytest.raises(CategorizedError) as exc:
        _engine().set_watchpoint(
            _attachment(controller, tmp_path), symbol=None, address=bad, byte_count=8
        )
    assert exc.value.details["code"] == "bad_address"
    assert controller.written == []


def test_set_watchpoint_unsupported_target_is_categorized(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-break-watch *(char(*)[8])0x1000": [
                {
                    "type": "result",
                    "message": "error",
                    "payload": {"msg": "Target does not support hardware watchpoints."},
                }
            ]
        }
    )
    with pytest.raises(CategorizedError) as exc:
        _engine().set_watchpoint(
            _attachment(controller, tmp_path), symbol=None, address=0x1000, byte_count=8
        )
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details["code"] == "watchpoint_unsupported"


def test_set_watchpoint_running_target_is_inferior_running(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-break-watch *(char(*)[8])0x1000": [
                {
                    "type": "result",
                    "message": "error",
                    "payload": {"msg": "Cannot insert watchpoints while the target is running."},
                }
            ]
        }
    )
    with pytest.raises(CategorizedError) as exc:
        _engine().set_watchpoint(
            _attachment(controller, tmp_path), symbol=None, address=0x1000, byte_count=8
        )
    assert exc.value.details["code"] == "inferior_running"


def test_set_watchpoint_malformed_result_is_categorized(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-break-watch *(char(*)[8])0x1000": [
                {"type": "result", "message": "done", "payload": {"no-wpt": {}}}
            ]
        }
    )
    with pytest.raises(CategorizedError) as exc:
        _engine().set_watchpoint(
            _attachment(controller, tmp_path), symbol=None, address=0x1000, byte_count=8
        )
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details["code"] == "no_watchpoint_record"


def test_set_watchpoint_passes_through_unrelated_gdb_error(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-break-watch *(char(*)[8])0x1000": [
                {"type": "result", "message": "error", "payload": {"msg": "Some other failure"}}
            ]
        }
    )
    with pytest.raises(CategorizedError) as exc:
        _engine().set_watchpoint(
            _attachment(controller, tmp_path), symbol=None, address=0x1000, byte_count=8
        )
    assert exc.value.details.get("code") not in {"watchpoint_unsupported", "inferior_running"}


def test_list_watchpoints_filters_watchpoint_rows(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-break-list": [
                {
                    "type": "result",
                    "message": "done",
                    "payload": {
                        "BreakpointTable": {
                            "body": [
                                {"bkpt": {"number": "1", "type": "breakpoint", "func": "panic"}},
                                {
                                    "bkpt": {
                                        "number": "2",
                                        "type": "hw watchpoint",
                                        "what": "*(char(*)[8])0x1000",
                                        "enabled": "y",
                                    }
                                },
                            ]
                        }
                    },
                }
            ]
        }
    )
    refs = _engine().list_watchpoints(_attachment(controller, tmp_path))
    assert [r.number for r in refs] == ["2"]
    assert refs[0].expr == "*(char(*)[8])0x1000"
    assert refs[0].enabled is True


def test_clear_watchpoint_requires_numeric_id(tmp_path: Path) -> None:
    controller = _FakeMiController()
    with pytest.raises(CategorizedError) as exc:
        _engine().clear_watchpoint(_attachment(controller, tmp_path), "abc")
    assert exc.value.details["code"] == "bad_watchpoint_id"
    assert controller.written == []


def test_clear_watchpoint_deletes(tmp_path: Path) -> None:
    controller = _FakeMiController()
    _engine().clear_watchpoint(_attachment(controller, tmp_path), "2")
    assert "-break-delete 2" in controller.written


def test_set_watchpoint_redacts_registered_secret_in_expr(tmp_path: Path) -> None:
    secret = "topsecretexpr"  # pragma: allowlist secret - fake test value
    controller = _FakeMiController(
        responses={
            "-break-watch *(char(*)[8])0x1000": [
                {
                    "type": "result",
                    "message": "done",
                    "payload": {"wpt": {"number": "2", "exp": secret}},
                }
            ]
        }
    )
    engine = _engine(Redactor(secret_values=[secret], registry=SecretRegistry()))
    ref = engine.set_watchpoint(
        _attachment(controller, tmp_path), symbol=None, address=0x1000, byte_count=8
    )
    assert ref.expr is not None
    assert secret not in ref.expr


# --- error mapping ------------------------------------------------------------------------


def test_run_maps_mi_error_to_debug_attach_failure(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-break-insert panic": [
                {"type": "result", "message": "error", "payload": {"msg": "no symbol"}}
            ]
        }
    )
    with pytest.raises(CategorizedError) as exc:
        _engine().set_breakpoint(_attachment(controller, tmp_path), "panic")
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details["command"] == "-break-insert panic"
    assert exc.value.details["payload"] == {"msg": "no symbol"}
    assert str(exc.value) == "gdb/MI command failed: -break-insert panic"


def test_execute_mi_command_passes_command_timeout_to_write(tmp_path: Path) -> None:
    controller = _FakeMiController()
    _engine().execute_mi_command(_attachment(controller, tmp_path), "-break-list")
    assert controller.write_timeouts == [10.0]


def test_execute_mi_command_returns_records_when_no_error(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={"-break-list": [{"type": "result", "message": "done", "payload": None}]}
    )
    records = _engine().execute_mi_command(_attachment(controller, tmp_path), "-break-list")
    assert [r.message for r in records] == ["done"]


def test_continue_raises_session_exited_on_terminal_stop(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={"-exec-continue": [{"type": "result", "message": "running", "payload": None}]},
        reads=[
            [{"type": "notify", "message": "stopped", "payload": {"reason": "exited-normally"}}]
        ],
    )
    with pytest.raises(CategorizedError) as exc:
        _engine().continue_(_attachment(controller, tmp_path), timeout_sec=1)
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details == {"code": "session_exited", "reason": "exited-normally"}
    assert str(exc.value) == "gdb/MI inferior exited (exited-normally); the debug session is dead"


def test_stop_record_from_extracts_frame_thread_and_bkptno(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={"-exec-continue": [{"type": "result", "message": "running", "payload": None}]},
        reads=[
            [
                {
                    "type": "notify",
                    "message": "stopped",
                    "payload": {
                        "reason": "breakpoint-hit",
                        "bkptno": "1",
                        "stopped-threads": "all",
                        "frame": {
                            "level": "0",
                            "func": "panic",
                            "addr": "0xffffffff81000000",
                            "file": "kernel/panic.c",
                            "line": "42",
                        },
                    },
                }
            ]
        ],
    )
    stop = _engine().continue_(_attachment(controller, tmp_path), timeout_sec=1)
    assert stop.reason == "breakpoint-hit"
    assert stop.bkptno == "1"
    assert stop.stopped_thread == "all"
    assert stop.frame is not None
    assert stop.frame.func == "panic"
    assert stop.frame.line == 42


def test_redact_stop_masks_registered_secret_in_frame() -> None:
    registry = SecretRegistry()
    registry.register("topsecretfunc", scope=object())
    engine = _engine(Redactor(registry=registry))
    stop = GdbStopRecord(
        reason="breakpoint-hit",
        frame=GdbFrame(level=0, func="topsecretfunc", addr=None, file=None, line=1),
    )
    redacted = engine.redact_stop(stop)
    assert redacted.frame is not None
    assert redacted.frame.func is not None
    assert "topsecretfunc" not in redacted.frame.func
    assert redacted.reason == "breakpoint-hit"


# --- transcript ----------------------------------------------------------------------------


def test_execute_mi_command_appends_one_transcript_line_per_command(tmp_path: Path) -> None:
    attachment = _attachment(_FakeMiController(), tmp_path)
    _engine().execute_mi_command(attachment, "-break-list")
    lines = attachment.transcript_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["command"] == "-break-list"


# --- session registry ----------------------------------------------------------------------


def test_registry_register_get_reap(tmp_path: Path) -> None:
    registry = GdbMiSessionRegistry()
    attachment = _attachment(_FakeMiController(), tmp_path)
    registry.register("s1", attachment)
    assert registry.get("s1") is attachment
    assert registry.reap("s1") is attachment
    assert registry.get("s1") is None


def test_registry_reap_missing_returns_none() -> None:
    assert GdbMiSessionRegistry().reap("never-registered") is None


def test_registry_require_returns_registered_attachment(tmp_path: Path) -> None:
    registry = GdbMiSessionRegistry()
    attachment = _attachment(_FakeMiController(), tmp_path)
    registry.register("s1", attachment)
    assert registry.require("s1") is attachment


def test_registry_require_raises_no_live_session() -> None:
    with pytest.raises(CategorizedError) as exc:
        GdbMiSessionRegistry().require("missing")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["code"] == "no_live_session"
    assert exc.value.details["debug_session_id"] == "missing"
    assert (
        str(exc.value)
        == "no live gdb/MI session; the engine is gone (server restarted or session reaped)"
    )


# --- debuginfo resolver --------------------------------------------------------------------


class _RecordingFetch:
    """A fake object-store fetch that records its ref args and returns canned bytes (or raises)."""

    def __init__(self, data: bytes = b"", error: CategorizedError | None = None) -> None:
        self._data = data
        self._error = error
        self.refs: list[str] = []

    def __call__(self, ref: str) -> bytes:
        self.refs.append(ref)
        if self._error is not None:
            raise self._error
        return self._data


def test_resolve_fetches_present_ref_to_dest(tmp_path: Path) -> None:
    fetch = _RecordingFetch(data=b"ELFDATA")
    resolver = DebuginfoResolver(
        read_debuginfo_ref=lambda run_id: "local/runs/r1/vmlinux", fetch_object=fetch
    )
    dest = tmp_path / "vmlinux"
    result = resolver.resolve("r1", dest)
    assert result == dest
    assert dest.read_bytes() == b"ELFDATA"
    assert fetch.refs == ["local/runs/r1/vmlinux"]


def test_resolve_none_ref_raises_no_debuginfo_before_fetch(tmp_path: Path) -> None:
    fetch = _RecordingFetch(data=b"unused")
    resolver = DebuginfoResolver(read_debuginfo_ref=lambda run_id: None, fetch_object=fetch)
    dest = tmp_path / "vmlinux"
    with pytest.raises(CategorizedError) as exc:
        resolver.resolve("r1", dest)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details == {"run_id": "r1", "reason": "no_debuginfo"}
    assert str(exc.value) == (
        "the Run has no published debuginfo object; build the kernel before attaching gdb"
    )
    assert fetch.refs == []  # the absent debuginfo is caught before any fetch
    assert not dest.exists()


def test_resolve_propagates_fetch_error(tmp_path: Path) -> None:
    boom = CategorizedError(
        "object store unreachable", category=ErrorCategory.INFRASTRUCTURE_FAILURE
    )
    fetch = _RecordingFetch(error=boom)
    resolver = DebuginfoResolver(
        read_debuginfo_ref=lambda run_id: "local/runs/r1/vmlinux", fetch_object=fetch
    )
    dest = tmp_path / "vmlinux"
    with pytest.raises(CategorizedError) as exc:
        resolver.resolve("r1", dest)
    assert exc.value is boom  # re-raised unchanged
    assert not dest.exists()


def test_resolve_writes_to_dest_not_run_id_derived_path(tmp_path: Path) -> None:
    # The resolver writes where it is told; it computes no run_id-derived path itself (the private
    # per-attach staging dir is the seam's responsibility). A hostile run_id never reaches the path.
    fetch = _RecordingFetch(data=b"SYMBOLS")
    resolver = DebuginfoResolver(read_debuginfo_ref=lambda run_id: "key", fetch_object=fetch)
    dest = tmp_path / "custom-name"
    resolver.resolve("../../etc/passwd", dest)
    assert dest.read_bytes() == b"SYMBOLS"
    assert dest.parent == tmp_path
