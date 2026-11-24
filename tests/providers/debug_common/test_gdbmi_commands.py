"""Focused unit tests for shared gdb/MI command mixins."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.debug import GdbBreakpointRef, GdbMiAttachment, GdbWatchpointRef
from kdive.providers.shared.debug_common.gdbmi.commands.breakpoints import (
    GdbMiBreakpointCommands,
)
from kdive.providers.shared.debug_common.gdbmi.commands.disassembly import GdbMiDisassemblyCommands
from kdive.providers.shared.debug_common.gdbmi.commands.modules import GdbMiModuleCommands
from kdive.providers.shared.debug_common.gdbmi.commands.registers import GdbMiRegisterCommands
from kdive.providers.shared.debug_common.gdbmi.commands.stack import GdbMiStackCommands
from kdive.providers.shared.debug_common.gdbmi.commands.symbols import GdbMiSymbolCommands
from kdive.providers.shared.debug_common.gdbmi.commands.threads import GdbMiThreadCommands
from kdive.providers.shared.debug_common.gdbmi.commands.watchpoints import (
    GdbMiWatchpointCommands,
)
from kdive.providers.shared.debug_common.gdbmi.core.mi_protocol import MiRecord
from kdive.providers.shared.debug_common.gdbmi.policy.debuginfo import ModuleDebuginfo
from kdive.security.secrets.redaction import REDACTION, Redactor
from kdive.security.secrets.secret_registry import SecretRegistry


def _done(payload: dict[str, object]) -> list[MiRecord]:
    return [MiRecord(type="result", message="done", payload=payload)]


def _gdb_error(message: str) -> CategorizedError:
    return CategorizedError(
        "gdb/MI command failed",
        category=ErrorCategory.DEBUG_ATTACH_FAILURE,
        details={"payload": {"msg": message}},
    )


class _NoopController:
    def write(self, command: str, *, timeout_sec: float) -> list[dict[str, object]]:
        del command, timeout_sec
        return []

    def read(self, *, timeout_sec: float) -> list[dict[str, object]]:
        del timeout_sec
        return []

    def get_gdb_response(
        self, *, timeout_sec: float, raise_error_on_timeout: bool = True
    ) -> list[dict[str, object]]:
        del timeout_sec, raise_error_on_timeout
        return []

    def exit(self) -> None:
        return None


def _attachment() -> GdbMiAttachment:
    return GdbMiAttachment(
        controller=_NoopController(),
        rsp_host="127.0.0.1",
        rsp_port=1234,
        transcript_path=Path("/tmp/kdive-gdbmi-test.log"),
        run_id="run-1",
    )


class _CommandHost(
    GdbMiBreakpointCommands,
    GdbMiWatchpointCommands,
    GdbMiModuleCommands,
    GdbMiSymbolCommands,
    GdbMiRegisterCommands,
    GdbMiStackCommands,
    GdbMiThreadCommands,
    GdbMiDisassemblyCommands,
):
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.responses: dict[str, list[MiRecord]] = {}
        self.errors: dict[str, CategorizedError] = {}
        self._redactor_value = Redactor(registry=SecretRegistry())
        self._module_resolver: Callable[[GdbMiAttachment, str], ModuleDebuginfo] = (
            self._resolve_module
        )

    def execute_mi_command(self, attachment: GdbMiAttachment, command: str) -> list[MiRecord]:
        del attachment
        self.commands.append(command)
        error = self.errors.get(command)
        if error is not None:
            raise error
        return self.responses.get(command, _done({}))

    def _redactor(self) -> Redactor:
        return self._redactor_value

    def read_memory(self, attachment: GdbMiAttachment, *, address: int, byte_count: int) -> bytes:
        del attachment, address
        return b"\0" * byte_count

    def _mi_path(self, path: Path) -> str:
        return str(path)

    def _resolve_module(self, _attachment: GdbMiAttachment, module: str) -> ModuleDebuginfo:
        return ModuleDebuginfo(path=Path(f"/symbols/{module}.ko"), srcversion="SRC", build_id=None)


def _script_module_list(host: _CommandHost, module: str | None) -> None:
    host.responses.update(
        {
            '-data-evaluate-expression "&((struct module *)0)->list"': _done({"value": "0x8"}),
            '-data-evaluate-expression "&modules"': _done({"value": "0x1000"}),
            '-data-evaluate-expression "modules.next"': _done(
                {"value": "0x3008" if module is not None else "0x1000"}
            ),
        }
    )
    if module is not None:
        host.responses.update(
            {
                '-data-evaluate-expression "((struct module *)0x3000)->name"': _done(
                    {"value": f'"{module}"'}
                ),
                '-data-evaluate-expression "((struct module *)0x3000)->mem[0].base"': _done(
                    {"value": "0x2000"}
                ),
                '-data-evaluate-expression "((struct list_head *)0x3008)->next"': _done(
                    {"value": "0x1000"}
                ),
            }
        )


def test_breakpoint_commands_build_expected_mi_strings() -> None:
    host = _CommandHost()
    attachment = _attachment()
    host.responses["-break-insert panic"] = _done(
        {"bkpt": {"number": "1", "type": "breakpoint", "func": "panic"}}
    )

    ref = host.set_breakpoint(attachment, "panic")
    host.clear_breakpoint(attachment, "1")

    assert ref.number == "1"
    assert host.commands == ["-break-insert panic", "-break-delete 1"]


def test_breakpoint_list_projects_breakpoint_rows() -> None:
    host = _CommandHost()
    host.responses["-break-list"] = _done(
        {
            "BreakpointTable": {
                "body": [
                    {"bkpt": {"number": "1", "type": "breakpoint", "func": "panic"}},
                    {"bkpt": {"number": "2", "type": "hw watchpoint", "what": "watched"}},
                ]
            }
        }
    )

    refs = host.list_breakpoints(_attachment())

    assert [(ref.number, ref.func, ref.what) for ref in refs] == [
        ("1", "panic", None),
        ("2", None, "watched"),
    ]
    assert host.commands == ["-break-list"]


@pytest.mark.parametrize(("location", "code"), [("panic()", "bad_location"), ("", "bad_location")])
def test_breakpoint_rejects_invalid_locations(location: str, code: str) -> None:
    host = _CommandHost()

    with pytest.raises(CategorizedError) as exc:
        host.set_breakpoint(_attachment(), location)

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["code"] == code


def test_breakpoint_rejects_non_numeric_clear_id() -> None:
    host = _CommandHost()

    with pytest.raises(CategorizedError) as exc:
        host.clear_breakpoint(_attachment(), "bkpt-1")

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details == {"code": "bad_breakpoint_id", "number": "bkpt-1"}
    assert host.commands == []


def test_breakpoint_insert_missing_record_is_debug_attach_failure() -> None:
    host = _CommandHost()
    host.responses["-break-insert panic"] = _done({})

    with pytest.raises(CategorizedError) as exc:
        host.set_breakpoint(_attachment(), "panic")

    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details == {"command_key": "bkpt"}


def test_watchpoint_commands_build_expected_mi_strings_and_filter_list() -> None:
    host = _CommandHost()
    attachment = _attachment()
    host.responses["-data-evaluate-expression &watched_value"] = _done({"value": "0x1234"})
    watch_command = "-break-watch *(char(*)[4])0x1234"
    host.responses[watch_command] = _done(
        {"wpt": {"number": "2", "type": "hw watchpoint", "exp": "*(char(*)[4])0x1234"}}
    )
    host.responses["-break-list"] = _done(
        {
            "BreakpointTable": {
                "body": [
                    {"bkpt": {"number": "1", "type": "breakpoint", "func": "panic"}},
                    {"bkpt": {"number": "2", "type": "hw watchpoint", "what": "watched"}},
                ]
            }
        }
    )

    ref = host.set_watchpoint(attachment, symbol="watched_value", byte_count=4)
    refs = host.list_watchpoints(attachment)
    host.clear_watchpoint(attachment, "2")

    assert ref.number == "2"
    assert [item.number for item in refs] == ["2"]
    assert host.commands == [
        "-data-evaluate-expression &watched_value",
        watch_command,
        "-break-list",
        "-break-delete 2",
    ]


@pytest.mark.parametrize(("byte_count", "code"), [(3, "bad_byte_count"), ("8", "bad_byte_count")])
def test_watchpoint_rejects_invalid_byte_counts(byte_count: object, code: str) -> None:
    host = _CommandHost()

    with pytest.raises(CategorizedError) as exc:
        host.set_watchpoint(_attachment(), address=0x1000, byte_count=cast(Any, byte_count))

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["code"] == code


def test_watchpoint_rejects_non_numeric_clear_id() -> None:
    host = _CommandHost()

    with pytest.raises(CategorizedError) as exc:
        host.clear_watchpoint(_attachment(), "wp-1")

    assert exc.value.details["code"] == "bad_watchpoint_id"


def test_watchpoint_running_target_error_is_remapped() -> None:
    host = _CommandHost()
    command = "-break-watch *(char(*)[8])0x1000"
    host.errors[command] = _gdb_error("The program is running. Try interrupting it.")

    with pytest.raises(CategorizedError) as exc:
        host.set_watchpoint(_attachment(), address=0x1000)

    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details == {"code": "inferior_running", "command": command}


def test_watchpoint_unsupported_error_is_remapped() -> None:
    host = _CommandHost()
    command = "-break-watch *(char(*)[8])0x1000"
    host.errors[command] = _gdb_error("Target does not support hardware watchpoint")

    with pytest.raises(CategorizedError) as exc:
        host.set_watchpoint(_attachment(), address=0x1000)

    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details == {"code": "watchpoint_unsupported", "command": command}


def test_module_not_loaded_is_classified() -> None:
    host = _CommandHost()
    _script_module_list(host, None)

    with pytest.raises(CategorizedError) as exc:
        host.load_module_symbols(_attachment(), module="nf_conntrack")

    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details == {"code": "module_not_loaded", "module": "nf_conntrack"}


def test_stale_module_base_is_classified() -> None:
    host = _CommandHost()
    _script_module_list(host, "nf_conntrack")

    with pytest.raises(CategorizedError) as exc:
        host.load_module_symbols(_attachment(), module="nf_conntrack", expected_base=0x1000)

    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details == {
        "code": "stale_module_address",
        "module": "nf_conntrack",
        "current_base": 0x2000,
    }


def test_load_module_symbols_adds_symbol_file_and_marks_loaded() -> None:
    host = _CommandHost()
    attachment = _attachment()
    _script_module_list(host, "nf_conntrack")
    host.responses['-data-evaluate-expression "((struct module *)0x3000)->srcversion"'] = _done(
        {"value": '"SRC"'}
    )

    module = host.load_module_symbols(attachment, module="nf_conntrack", expected_base=0x2000)

    assert module.name == "nf_conntrack"
    assert module.base_address == "0x2000"
    assert module.symbols_loaded is True
    assert module.identity_verified is True
    assert attachment.loaded_modules == {"nf_conntrack"}
    assert host.commands == [
        '-data-evaluate-expression "&((struct module *)0)->list"',
        '-data-evaluate-expression "&modules"',
        '-data-evaluate-expression "modules.next"',
        '-data-evaluate-expression "((struct module *)0x3000)->name"',
        '-data-evaluate-expression "((struct module *)0x3000)->mem[0].base"',
        '-data-evaluate-expression "((struct list_head *)0x3008)->next"',
        '-data-evaluate-expression "((struct module *)0x3000)->srcversion"',
        '-interpreter-exec console "add-symbol-file /symbols/nf_conntrack.ko 0x2000"',
    ]


def test_resolve_symbol_evaluates_address_of_identifier() -> None:
    host = _CommandHost()
    host.responses["-data-evaluate-expression &panic"] = _done(
        {"value": "0xffffffff81000000 <panic>"}
    )

    address = host.resolve_symbol(_attachment(), "panic")

    assert address == 0xFFFFFFFF81000000
    assert host.commands == ["-data-evaluate-expression &panic"]


@pytest.mark.parametrize("name", ["panic()", "module.name", ""])
def test_resolve_symbol_rejects_non_identifier_names(name: str) -> None:
    host = _CommandHost()

    with pytest.raises(CategorizedError) as exc:
        host.resolve_symbol(_attachment(), name)

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details == {"code": "bad_symbol_name", "name": name}
    assert host.commands == []


def test_resolve_symbol_maps_gdb_not_found_error() -> None:
    host = _CommandHost()
    host.errors["-data-evaluate-expression &missing_symbol"] = _gdb_error(
        "No symbol missing_symbol in current context."
    )

    with pytest.raises(CategorizedError) as exc:
        host.resolve_symbol(_attachment(), "missing_symbol")

    assert exc.value.category is ErrorCategory.SYMBOL_NOT_FOUND
    assert exc.value.details == {
        "code": "symbol_not_found",
        "name": "missing_symbol",
        "hint": "symbol may be inlined or optimized away; try disassembling its caller.",
    }


def test_resolve_symbol_redacts_unparseable_value() -> None:
    host = _CommandHost()
    host._redactor_value = Redactor(secret_values=["secret-address"], registry=SecretRegistry())
    host.responses["-data-evaluate-expression &panic"] = _done({"value": "secret-address"})

    with pytest.raises(CategorizedError) as exc:
        host.resolve_symbol(_attachment(), "panic")

    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details == {
        "code": "bad_symbol_value",
        "name": "panic",
        "value": REDACTION,
    }


def test_read_registers_maps_requested_names_to_values() -> None:
    host = _CommandHost()
    host._redactor_value = Redactor(secret_values=["secret-register"], registry=SecretRegistry())
    host.responses["-data-list-register-names"] = _done({"register-names": ["rax", "rbx", "rip"]})
    host.responses["-data-list-register-values x"] = _done(
        {
            "register-values": [
                {"number": "0", "value": "0x1"},
                {"number": "2", "value": "secret-register"},
            ]
        }
    )

    registers = host.read_registers(_attachment(), ["rip", "rax"])

    assert registers == {"rip": REDACTION, "rax": "0x1"}
    assert host.commands == ["-data-list-register-names", "-data-list-register-values x"]


@pytest.mark.parametrize("names", [[], ["rax", "rip=0x1"]])
def test_read_registers_rejects_invalid_requests(names: list[str]) -> None:
    host = _CommandHost()

    with pytest.raises(CategorizedError) as exc:
        host.read_registers(_attachment(), names)

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["code"] == "bad_register"
    assert host.commands == []


def test_read_registers_reports_missing_requested_values() -> None:
    host = _CommandHost()
    host.responses["-data-list-register-names"] = _done({"register-names": ["rax", "rip"]})
    host.responses["-data-list-register-values x"] = _done(
        {"register-values": [{"number": "0", "value": "0x1"}]}
    )

    with pytest.raises(CategorizedError) as exc:
        host.read_registers(_attachment(), ["rip", "rax"])

    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details == {
        "code": "missing_registers",
        "requested": ["rip", "rax"],
        "missing": ["rip"],
    }


def _bare_host(host: _CommandHost) -> Any:
    return SimpleNamespace(
        execute_mi_command=host.execute_mi_command,
        _redactor=host._redactor,
        _module_resolver=host._module_resolver,
        read_memory=host.read_memory,
        _mi_path=host._mi_path,
        read_thread_id=host.read_thread_id,
        resolve_symbol=host.resolve_symbol,
    )


def test_breakpoint_runs_without_inherited_helpers() -> None:
    host = _CommandHost()
    host.responses["-break-insert panic"] = _done({"bkpt": {"number": "1"}})

    result = GdbMiBreakpointCommands.set_breakpoint(_bare_host(host), _attachment(), "panic")

    assert result.number == "1"
    assert host.commands == ["-break-insert panic"]


def test_symbol_lookup_runs_without_inherited_helpers() -> None:
    host = _CommandHost()
    host.responses["-data-evaluate-expression &panic"] = _done({"value": "0x1234"})

    assert GdbMiSymbolCommands.resolve_symbol(_bare_host(host), _attachment(), "panic") == 0x1234
    assert host.commands == ["-data-evaluate-expression &panic"]


def test_module_listing_runs_without_inherited_helpers() -> None:
    host = _CommandHost()
    _script_module_list(host, None)

    result = GdbMiModuleCommands.list_modules(_bare_host(host), _attachment())

    assert result.modules == []
    assert not result.truncated
    assert result.decode_errors == 0
    assert host.commands == [
        '-data-evaluate-expression "&((struct module *)0)->list"',
        '-data-evaluate-expression "&modules"',
        '-data-evaluate-expression "modules.next"',
    ]


def test_stack_walk_runs_without_inherited_helpers() -> None:
    host = _CommandHost()
    host.responses["-stack-list-frames"] = _done(
        {"stack": [{"level": "0", "func": "panic", "addr": "0x1000"}]}
    )

    result = GdbMiStackCommands.backtrace(_bare_host(host), _attachment())

    assert len(result.frames) == 1
    assert result.frames[0].func == "panic"
    assert host.commands == ["-stack-list-frames"]


def test_disassembly_runs_without_inherited_helpers() -> None:
    host = _CommandHost()
    command = "-data-disassemble -s 0x0 -e 0x10 -- 0"
    host.responses[command] = _done({"asm_insns": [{"address": "0x0", "inst": "nop"}]})

    result = GdbMiDisassemblyCommands.disassemble(
        _bare_host(host), _attachment(), address=0, instruction_count=1
    )

    assert len(result.instructions) == 1
    assert result.instructions[0].inst == "nop"
    assert host.commands == [command]


def test_watchpoint_runs_without_inherited_helpers() -> None:
    host = _CommandHost()
    command = "-break-watch *(char(*)[8])0x0"
    host.responses[command] = _done({"wpt": {"number": "2", "type": "hw watchpoint"}})

    result = GdbMiWatchpointCommands.set_watchpoint(_bare_host(host), _attachment(), address=0)

    assert result.number == "2"
    assert host.commands == [command]


def test_module_listing_redacts_registered_name() -> None:
    host = _CommandHost()
    host._redactor_value = Redactor(secret_values=["secret_module"], registry=SecretRegistry())
    _script_module_list(host, "secret_module")

    result = host.list_modules(_attachment())

    assert len(result.modules) == 1
    assert result.modules[0].name == REDACTION


_MISSING_REFERENCE_NUMBER = object()


@pytest.mark.parametrize("family", ["breakpoint", "watchpoint"])
@pytest.mark.parametrize("shape", ["insert", "wrapped", "bare"])
@pytest.mark.parametrize(
    ("number", "usable"),
    [
        pytest.param("0", True, id="zero"),
        pytest.param("1", True, id="numeric"),
        pytest.param("001", True, id="leading-zero"),
        pytest.param("1.2", True, id="location"),
        pytest.param("001.02", True, id="zero-location"),
        pytest.param(_MISSING_REFERENCE_NUMBER, False, id="missing"),
        pytest.param(None, False, id="null"),
        pytest.param(1, False, id="integer"),
        pytest.param(True, False, id="boolean"),
        pytest.param([], False, id="list"),
        pytest.param({}, False, id="mapping"),
        pytest.param("", False, id="empty"),
        pytest.param("None", False, id="fabricated-none"),
        pytest.param("private_number", False, id="untrusted-text"),
        pytest.param(" 1", False, id="leading-space"),
        pytest.param("1 ", False, id="trailing-space"),
        pytest.param("1\n", False, id="trailing-newline"),
        pytest.param("١", False, id="non-ascii-digit"),
        pytest.param("1.2.3", False, id="multiple-dots"),
        pytest.param(".1", False, id="missing-major"),
        pytest.param("1.", False, id="missing-minor"),
    ],
)
def test_reference_number_contract(family: str, shape: str, number: object, usable: bool) -> None:
    host = _CommandHost()
    attachment = _attachment()
    host._redactor_value = Redactor(secret_values=["private_target"], registry=SecretRegistry())
    kind = "breakpoint" if family == "breakpoint" else "hw watchpoint"
    entry: dict[str, object] = {
        "type": kind,
        "addr": "0x1234",
        "func": "private_target",
        "what": "private_target",
        "exp": "private_target",
        "enabled": "y",
    }
    if number is not _MISSING_REFERENCE_NUMBER:
        entry["number"] = number
    if shape == "insert":
        command = (
            "-break-insert panic" if family == "breakpoint" else "-break-watch *(char(*)[8])0x0"
        )
        host.responses[command] = _done({"bkpt" if family == "breakpoint" else "wpt": entry})
    else:
        row = {"bkpt": entry} if shape == "wrapped" else entry
        host.responses["-break-list"] = _done(
            {"BreakpointTable": {"body": [{"bkpt": {"number": "7", "type": kind}}, row]}}
        )

    def read() -> Sequence[GdbBreakpointRef | GdbWatchpointRef]:
        if shape == "insert":
            if family == "breakpoint":
                return [host.set_breakpoint(attachment, "panic")]
            return [host.set_watchpoint(attachment, address=0)]
        if family == "breakpoint":
            return host.list_breakpoints(attachment)
        return host.list_watchpoints(attachment)

    if shape == "bare" and number is _MISSING_REFERENCE_NUMBER:
        assert [ref.number for ref in read()] == ["7"]
    elif usable:
        refs = read()
        assert [ref.number for ref in refs] == ([number] if shape == "insert" else ["7", number])
        ref = refs[-1]
        assert ref.type == kind and ref.addr == "0x1234"
        if isinstance(ref, GdbBreakpointRef):
            assert ref.func == REDACTION and ref.what == REDACTION and ref.enabled is None
        else:
            assert ref.expr == REDACTION and ref.enabled is True
    else:
        with pytest.raises(CategorizedError) as exc:
            read()
        assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
        assert exc.value.details == {}
        assert "private_number" not in str(exc.value)
