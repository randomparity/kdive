"""Kernel module symbol command family for shared GDB/MI debugging."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.debug import GdbMiAttachment, GdbModule, GdbModuleList
from kdive.providers.shared.debug_common.gdbmi.debuginfo import (
    ModuleDebuginfo,
    ModuleDebuginfoResolverSeam,
)
from kdive.providers.shared.debug_common.gdbmi.mi_protocol import MiRecord, evaluate_value
from kdive.security.secrets.redaction import Redactor

MAX_MODULES = 512
_MODULE_BASE_FIELDS = ("mem[0].base", "core_layout.base")
_MODULE_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
_BUILD_ID_BYTES = 20
_RUNNING_RE = re.compile(r"running", re.IGNORECASE)
_ADDR_RE = re.compile(r"0x[0-9a-fA-F]+")


class _ModuleHost(Protocol):
    _module_resolver: ModuleDebuginfoResolverSeam

    def execute_mi_command(self, attachment: GdbMiAttachment, command: str) -> list[MiRecord]: ...

    def _redactor(self) -> Redactor: ...

    def read_memory(
        self, attachment: GdbMiAttachment, *, address: int, byte_count: int
    ) -> bytes: ...

    def _mi_path(self, path: Path) -> str: ...

    def _module_walk(
        self, attachment: GdbMiAttachment, *, limit: int
    ) -> tuple[list[tuple[str, int, int]], bool, int]: ...

    def _module_eval_required(self, attachment: GdbMiAttachment, expr: str) -> int: ...

    def _module_eval_optional(self, attachment: GdbMiAttachment, expr: str) -> int | None: ...

    def _module_name(self, attachment: GdbMiAttachment, module_ptr: int) -> str | None: ...

    def _module_base(
        self, attachment: GdbMiAttachment, module_ptr: int, base_field: str
    ) -> tuple[int | None, str]: ...

    def _module_walk_error(self, exc: CategorizedError) -> CategorizedError: ...

    def _redact_module(self, module: GdbModule) -> GdbModule: ...

    def _module_locate(
        self, attachment: GdbMiAttachment, module: str
    ) -> tuple[int, int] | None: ...

    def _verify_identity(
        self, attachment: GdbMiAttachment, module: str, module_ptr: int, info: ModuleDebuginfo
    ) -> bool: ...

    def _identity_match(self, module: str, ok: bool) -> bool: ...

    def _module_text_field(
        self, attachment: GdbMiAttachment, module_ptr: int, field: str
    ) -> str | None: ...

    def _module_build_id(self, attachment: GdbMiAttachment, module_ptr: int) -> str | None: ...

    def _add_symbol_file(
        self, attachment: GdbMiAttachment, module: str, path: Path, base: int
    ) -> None: ...


def _config_error(
    message: str, *, code: str, details: dict[str, object] | None = None
) -> CategorizedError:
    merged: dict[str, object] = {"code": code, **(details or {})}
    return CategorizedError(message, category=ErrorCategory.CONFIGURATION_ERROR, details=merged)


class GdbMiModuleCommands:
    """Kernel module enumeration and symbol loading commands."""

    def list_modules(
        self: _ModuleHost,
        attachment: GdbMiAttachment,
        *,
        max_modules: int = MAX_MODULES,
    ) -> GdbModuleList:
        """Walk the kernel ``modules`` list into a bounded, redacted module list (ADR-0278)."""
        rows, truncated, decode_errors = self._module_walk(attachment, limit=max_modules)
        modules = [
            self._redact_module(
                GdbModule(
                    name=name,
                    base_address=f"0x{base:x}",
                    symbols_loaded=name in attachment.loaded_modules,
                )
            )
            for name, base, _ptr in rows
        ]
        return GdbModuleList(modules=modules, truncated=truncated, decode_errors=decode_errors)

    def _module_walk(
        self: _ModuleHost, attachment: GdbMiAttachment, *, limit: int
    ) -> tuple[list[tuple[str, int, int]], bool, int]:
        """Walk ``modules`` from ``modules.next`` to ``&modules``, bounded to ``limit``."""
        offset = self._module_eval_required(attachment, "&((struct module *)0)->list")
        head = self._module_eval_required(attachment, "&modules")
        node = self._module_eval_required(attachment, "modules.next")
        rows: list[tuple[str, int, int]] = []
        decode_errors = 0
        base_field = ""
        truncated = False
        processed = 0
        while node != head:
            if processed >= limit:
                truncated = True
                break
            processed += 1
            module_ptr = (node - offset) & 0xFFFFFFFFFFFFFFFF
            name = self._module_name(attachment, module_ptr)
            base, base_field = self._module_base(attachment, module_ptr, base_field)
            if name is not None and base is not None:
                rows.append((name, base, module_ptr))
            else:
                decode_errors += 1
            nxt = self._module_eval_optional(attachment, f"((struct list_head *)0x{node:x})->next")
            if nxt is None or nxt == node:
                break
            node = nxt
        return rows, truncated, decode_errors

    def _module_eval_required(self: _ModuleHost, attachment: GdbMiAttachment, expr: str) -> int:
        try:
            value = evaluate_value(self.execute_mi_command(attachment, _eval_command(expr)))
        except CategorizedError as exc:
            raise self._module_walk_error(exc) from exc
        addr = _hex_from(value)
        if addr is None:
            raise CategorizedError(
                "gdb/MI returned no parseable module-list pointer",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"code": "module_decode_failed", "expr": expr},
            )
        return addr

    def _module_eval_optional(
        self: _ModuleHost, attachment: GdbMiAttachment, expr: str
    ) -> int | None:
        try:
            value = evaluate_value(self.execute_mi_command(attachment, _eval_command(expr)))
        except CategorizedError as exc:
            _raise_unless_decode_failure(exc)
            return None
        return _hex_from(value)

    def _module_name(self: _ModuleHost, attachment: GdbMiAttachment, module_ptr: int) -> str | None:
        try:
            value = evaluate_value(
                self.execute_mi_command(
                    attachment,
                    _eval_command(f"((struct module *)0x{module_ptr:x})->name"),
                )
            )
        except CategorizedError as exc:
            _raise_unless_decode_failure(exc)
            return None
        match = re.search(r'"([^"]*)"', value) if isinstance(value, str) else None
        name = match.group(1) if match is not None else None
        return name or None

    def _module_base(
        self: _ModuleHost,
        attachment: GdbMiAttachment,
        module_ptr: int,
        base_field: str,
    ) -> tuple[int | None, str]:
        if base_field:
            expr = f"((struct module *)0x{module_ptr:x})->{base_field}"
            return self._module_eval_optional(attachment, expr), base_field
        for candidate in _MODULE_BASE_FIELDS:
            expr = f"((struct module *)0x{module_ptr:x})->{candidate}"
            base = self._module_eval_optional(attachment, expr)
            if base is not None:
                return base, candidate
        raise CategorizedError(
            "gdb/MI could not read any known module base-address field",
            category=ErrorCategory.DEBUG_ATTACH_FAILURE,
            details={"code": "module_decode_failed", "fields": list(_MODULE_BASE_FIELDS)},
        )

    def _module_walk_error(self: _ModuleHost, exc: CategorizedError) -> CategorizedError:
        payload = exc.details.get("payload")
        msg = payload.get("msg") if isinstance(payload, dict) else None
        if isinstance(msg, str) and _RUNNING_RE.search(msg):
            return CategorizedError(
                "gdb/MI cannot read the module list while the inferior is running",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"code": "inferior_running"},
            )
        return CategorizedError(
            "gdb/MI could not read the kernel module list",
            category=ErrorCategory.DEBUG_ATTACH_FAILURE,
            details={"code": "module_decode_failed"},
        )

    def _redact_module(self: _ModuleHost, module: GdbModule) -> GdbModule:
        return GdbModule.model_validate(
            self._redactor().redact_value(module.model_dump(mode="json"))
        )

    def load_module_symbols(
        self: _ModuleHost,
        attachment: GdbMiAttachment,
        *,
        module: str,
        expected_base: int | None = None,
    ) -> GdbModule:
        """Load one module's symbols at its freshly-read base, identity-checked (ADR-0278)."""
        if not _MODULE_NAME_RE.match(module):
            raise _config_error(
                f"module name must be a bare identifier, got {module!r}",
                code="bad_module_name",
                details={"module": module},
            )
        located = self._module_locate(attachment, module)
        if located is None:
            raise CategorizedError(
                f"module {module!r} is not in the live module list (unloaded or never loaded)",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"code": "module_not_loaded", "module": module},
            )
        base, module_ptr = located
        if expected_base is not None and expected_base != base:
            raise CategorizedError(
                f"module {module!r} base changed since listing (reloaded); refusing a stale load",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"code": "stale_module_address", "module": module, "current_base": base},
            )
        if module in attachment.loaded_modules:
            return self._redact_module(
                GdbModule(name=module, base_address=f"0x{base:x}", symbols_loaded=True)
            )
        info = self._module_resolver(attachment.run_id, module)
        verified = self._verify_identity(attachment, module, module_ptr, info)
        self._add_symbol_file(attachment, module, info.path, base)
        attachment.loaded_modules.add(module)
        return self._redact_module(
            GdbModule(
                name=module,
                base_address=f"0x{base:x}",
                symbols_loaded=True,
                identity_verified=verified,
            )
        )

    def _module_locate(
        self: _ModuleHost, attachment: GdbMiAttachment, module: str
    ) -> tuple[int, int] | None:
        rows, _truncated, _decode_errors = self._module_walk(attachment, limit=MAX_MODULES)
        for name, base, module_ptr in rows:
            if name == module:
                return base, module_ptr
        return None

    def _verify_identity(
        self: _ModuleHost,
        attachment: GdbMiAttachment,
        module: str,
        module_ptr: int,
        info: ModuleDebuginfo,
    ) -> bool:
        live_src = self._module_text_field(attachment, module_ptr, "srcversion")
        if live_src is not None and info.srcversion is not None:
            return self._identity_match(module, live_src == info.srcversion)
        live_bid = self._module_build_id(attachment, module_ptr)
        if live_bid is not None and info.build_id is not None:
            return self._identity_match(module, live_bid.startswith(info.build_id))
        return False

    def _identity_match(self, module: str, ok: bool) -> bool:
        if ok:
            return True
        raise CategorizedError(
            f"the artifact .ko for {module!r} does not match the running module's identity",
            category=ErrorCategory.DEBUG_ATTACH_FAILURE,
            details={"code": "module_binary_mismatch", "module": module},
        )

    def _module_text_field(
        self: _ModuleHost, attachment: GdbMiAttachment, module_ptr: int, field: str
    ) -> str | None:
        try:
            value = evaluate_value(
                self.execute_mi_command(
                    attachment,
                    _eval_command(f"((struct module *)0x{module_ptr:x})->{field}"),
                )
            )
        except CategorizedError as exc:
            _raise_unless_decode_failure(exc)
            return None
        match = re.search(r'"([^"]*)"', value) if isinstance(value, str) else None
        return match.group(1) if match is not None and match.group(1) else None

    def _module_build_id(
        self: _ModuleHost, attachment: GdbMiAttachment, module_ptr: int
    ) -> str | None:
        addr = self._module_eval_optional(
            attachment, f"&((struct module *)0x{module_ptr:x})->build_id"
        )
        if addr is None:
            return None
        try:
            blob = self.read_memory(attachment, address=addr, byte_count=_BUILD_ID_BYTES)
        except CategorizedError as exc:
            _raise_unless_decode_failure(exc)
            return None
        return blob.hex()

    def _add_symbol_file(
        self: _ModuleHost,
        attachment: GdbMiAttachment,
        module: str,
        path: Path,
        base: int,
    ) -> None:
        text = self._mi_path(path)
        if '"' in text:
            raise _config_error(
                "staged module path is not safely quotable", code="add_symbol_failed"
            )
        command = f'-interpreter-exec console "add-symbol-file {text} 0x{base:x}"'
        try:
            self.execute_mi_command(attachment, command)
        except CategorizedError as exc:
            raise CategorizedError(
                f"gdb/MI add-symbol-file failed for {module!r}",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"code": "add_symbol_failed", "module": module},
            ) from exc


def _raise_unless_decode_failure(exc: CategorizedError) -> None:
    if exc.category is not ErrorCategory.DEBUG_ATTACH_FAILURE:
        raise exc


def _eval_command(expr: str) -> str:
    """The ``-data-evaluate-expression`` MI command for ``expr``, quoted as one MI argument."""
    return f'-data-evaluate-expression "{expr}"'


def _hex_from(value: object) -> int | None:
    """The first ``0x...`` token in a gdb-rendered pointer value, as an int."""
    if not isinstance(value, str):
        return None
    match = _ADDR_RE.search(value)
    return int(match.group(0), 16) if match is not None else None
