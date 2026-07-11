"""Focused unit tests for shared gdb/MI command mixins."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.debug import GdbMiAttachment
from kdive.providers.shared.debug_common.gdbmi.commands.breakpoints import (
    GdbMiBreakpointCommands,
)
from kdive.providers.shared.debug_common.gdbmi.commands.modules import GdbMiModuleCommands
from kdive.providers.shared.debug_common.gdbmi.commands.watchpoints import (
    GdbMiWatchpointCommands,
)
from kdive.providers.shared.debug_common.gdbmi.core.mi_protocol import MiRecord
from kdive.providers.shared.debug_common.gdbmi.policy.debuginfo import ModuleDebuginfo
from kdive.security.secrets.redaction import Redactor
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
):
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.responses: dict[str, list[MiRecord]] = {}
        self.errors: dict[str, CategorizedError] = {}
        self.module_rows: list[tuple[str, int, int]] = []
        self._redactor_value = Redactor(registry=SecretRegistry())
        self._module_resolver: Callable[[str, str], ModuleDebuginfo] = self._resolve_module

    def execute_mi_command(self, attachment: GdbMiAttachment, command: str) -> list[MiRecord]:
        del attachment
        self.commands.append(command)
        error = self.errors.get(command)
        if error is not None:
            raise error
        return self.responses.get(command, _done({}))

    def _redactor(self) -> Redactor:
        return self._redactor_value

    def _resolve_target(
        self, attachment: GdbMiAttachment, *, symbol: str | None, address: int | None
    ) -> int:
        del attachment
        if address is not None:
            return address
        if symbol == "watched_value":
            return 0x1234
        raise CategorizedError(
            "target missing",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"code": "bad_target"},
        )

    def _module_walk(
        self, attachment: GdbMiAttachment, *, limit: int
    ) -> tuple[list[tuple[str, int, int]], bool, int]:
        del attachment
        return self.module_rows[:limit], len(self.module_rows) > limit, 0

    def read_memory(self, attachment: GdbMiAttachment, *, address: int, byte_count: int) -> bytes:
        del attachment, address
        return b"\0" * byte_count

    def _mi_path(self, path: Path) -> str:
        return str(path)

    def _resolve_module(self, _run_id: str, module: str) -> ModuleDebuginfo:
        return ModuleDebuginfo(path=Path(f"/symbols/{module}.ko"), srcversion="SRC", build_id=None)


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


@pytest.mark.parametrize(("location", "code"), [("panic()", "bad_location"), ("", "bad_location")])
def test_breakpoint_rejects_invalid_locations(location: str, code: str) -> None:
    host = _CommandHost()

    with pytest.raises(CategorizedError) as exc:
        host.set_breakpoint(_attachment(), location)

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["code"] == code


def test_watchpoint_commands_build_expected_mi_strings_and_filter_list() -> None:
    host = _CommandHost()
    attachment = _attachment()
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
    assert host.commands == [watch_command, "-break-list", "-break-delete 2"]


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

    with pytest.raises(CategorizedError) as exc:
        host.load_module_symbols(_attachment(), module="nf_conntrack")

    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details == {"code": "module_not_loaded", "module": "nf_conntrack"}


def test_stale_module_base_is_classified() -> None:
    host = _CommandHost()
    host.module_rows = [("nf_conntrack", 0x2000, 0x3000)]

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
    host.module_rows = [("nf_conntrack", 0x2000, 0x3000)]
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
        '-data-evaluate-expression "((struct module *)0x3000)->srcversion"',
        '-interpreter-exec console "add-symbol-file /symbols/nf_conntrack.ko 0x2000"',
    ]
