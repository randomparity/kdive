"""Symbol-resolution command family for :mod:`kdive.providers.shared.debug_common.gdbmi`."""

from __future__ import annotations

import re
from typing import Protocol

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.debug import GdbMiAttachment
from kdive.providers.shared.debug_common.gdbmi.core.mi_protocol import MiRecord, evaluate_value
from kdive.security.secrets.redaction import Redactor

_SYMBOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SYMBOL_ADDR_RE = re.compile(r"0x[0-9a-fA-F]+")
_SYMBOL_NOT_FOUND_RE = re.compile(
    r"No symbol .* in current context|address of value not located in memory", re.IGNORECASE
)
_SYMBOL_INLINE_HINT = "symbol may be inlined or optimized away; try disassembling its caller."


class _SymbolHost(Protocol):
    def execute_mi_command(self, attachment: GdbMiAttachment, command: str) -> list[MiRecord]: ...

    def _redactor(self) -> Redactor: ...

    def _evaluate_symbol(self, attachment: GdbMiAttachment, name: str) -> list[MiRecord]: ...


def _config_error(
    message: str, *, code: str, details: dict[str, object] | None = None
) -> CategorizedError:
    merged: dict[str, object] = {"code": code, **(details or {})}
    return CategorizedError(message, category=ErrorCategory.CONFIGURATION_ERROR, details=merged)


class GdbMiSymbolCommands:
    """Symbol-address lookup GDB/MI commands."""

    def resolve_symbol(self: _SymbolHost, attachment: GdbMiAttachment, name: str) -> int:
        """Resolve a bare C symbol ``name`` to its address via ``-data-evaluate-expression``.

        The Run's DWARF ``vmlinux`` is already loaded at attach, so the symbol table is present.
        ``name`` is gated to a bare C identifier (``_SYMBOL_NAME_RE``) so the only expression
        ever evaluated is ``&<identifier>`` - address-of-a-name, never an arbitrary expression
        (non-injectable, the same property the breakpoint-location gate relies on). ``&name``
        resolves both data globals (e.g. ``d_hash_shift``) and functions, unlike
        ``-break-insert`` (a code location only). This is a symtab lookup, so it is valid whether
        or not the inferior is stopped.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` / ``bad_symbol_name`` for a non-identifier
                name (raised before any MI command); ``SYMBOL_NOT_FOUND`` (non-retryable, with the
                inline hint) when gdb cannot resolve the name to an address - an unknown / inlined /
                optimized-away symbol or an addressless enum/macro constant (ADR-0307);
                ``DEBUG_ATTACH_FAILURE`` for any other gdb error (e.g. debuginfo never loaded) or a
                present-but-unparseable address value (``bad_symbol_value``, value redacted).
        """
        if not _SYMBOL_NAME_RE.match(name):
            raise _config_error(
                f"symbol name must be a bare C identifier, got {name!r}",
                code="bad_symbol_name",
                details={"name": name},
            )
        value = evaluate_value(self._evaluate_symbol(attachment, name))
        match = _SYMBOL_ADDR_RE.search(value) if isinstance(value, str) else None
        if match is None:
            raise CategorizedError(
                "gdb/MI returned no parseable symbol address",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={
                    "code": "bad_symbol_value",
                    "name": name,
                    "value": self._redactor().redact_value(value),
                },
            )
        return int(match.group(0), 16)

    def _evaluate_symbol(
        self: _SymbolHost, attachment: GdbMiAttachment, name: str
    ) -> list[MiRecord]:
        """Evaluate ``&name``, narrowing a resolution ``^error`` to ``SYMBOL_NOT_FOUND``."""
        try:
            return self.execute_mi_command(attachment, f"-data-evaluate-expression &{name}")
        except CategorizedError as exc:
            if exc.category is ErrorCategory.DEBUG_ATTACH_FAILURE:
                payload = exc.details.get("payload")
                msg = payload.get("msg") if isinstance(payload, dict) else None
                if isinstance(msg, str) and _SYMBOL_NOT_FOUND_RE.search(msg):
                    raise CategorizedError(
                        f"symbol {name!r} not found",
                        category=ErrorCategory.SYMBOL_NOT_FOUND,
                        details={
                            "code": "symbol_not_found",
                            "name": name,
                            "hint": _SYMBOL_INLINE_HINT,
                        },
                    ) from exc
            raise
