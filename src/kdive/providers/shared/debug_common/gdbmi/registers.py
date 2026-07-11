"""Register read command family for :mod:`kdive.providers.shared.debug_common.gdbmi`."""

from __future__ import annotations

import re

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.debug import GdbMiAttachment
from kdive.providers.shared.debug_common.gdbmi.host import GdbMiCommandHost
from kdive.providers.shared.debug_common.gdbmi.mi_protocol import (
    register_names as parsed_register_names,
)
from kdive.providers.shared.debug_common.gdbmi.mi_protocol import (
    register_values_by_number,
)

_REGISTER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _config_error(
    message: str, *, code: str, details: dict[str, object] | None = None
) -> CategorizedError:
    merged: dict[str, object] = {"code": code, **(details or {})}
    return CategorizedError(message, category=ErrorCategory.CONFIGURATION_ERROR, details=merged)


class GdbMiRegisterCommands:
    """Register-read GDB/MI commands."""

    def read_registers(
        self: GdbMiCommandHost,
        attachment: GdbMiAttachment,
        register_names: list[str],
    ) -> dict[str, object]:
        if not isinstance(register_names, list) or not register_names:
            raise _config_error("registers must be a non-empty list", code="bad_register")
        requested: list[str] = []
        for name in register_names:
            if not isinstance(name, str) or not _REGISTER_RE.match(name):
                raise _config_error(f"invalid register name {name!r}", code="bad_register")
            requested.append(name)
        # gdb keys register VALUES by ordinal number; map names->ordinals via
        # -data-list-register-names, then return only the requested names.
        ordered_names = parsed_register_names(
            self.execute_mi_command(attachment, "-data-list-register-names")
        )
        by_number = register_values_by_number(
            self.execute_mi_command(attachment, "-data-list-register-values x")
        )
        registers: dict[str, object] = {}
        for name in requested:
            if name in ordered_names:
                ordinal = str(ordered_names.index(name))
                if ordinal in by_number:
                    registers[name] = by_number[ordinal]
        missing = [name for name in requested if name not in registers]
        if missing:
            raise CategorizedError(
                "gdb/MI omitted requested register data",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"code": "missing_registers", "requested": requested, "missing": missing},
            )
        redacted = self._redactor().redact_value(registers)
        return redacted if isinstance(redacted, dict) else {}
