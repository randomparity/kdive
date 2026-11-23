"""Kernel module symbol command family for shared GDB/MI debugging."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.debug import GdbMiAttachment, GdbModule, GdbModuleList
from kdive.providers.shared.debug_common.gdbmi._errors import config_error
from kdive.providers.shared.debug_common.gdbmi.core.mi_protocol import MiRecord, evaluate_value
from kdive.providers.shared.debug_common.gdbmi.policy.debuginfo import (
    ModuleDebuginfo,
    ModuleDebuginfoResolverSeam,
)
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


class GdbMiModuleCommands:
    """Kernel module enumeration and symbol loading commands."""

    def list_modules(
        self: _ModuleHost,
        attachment: GdbMiAttachment,
        *,
        max_modules: int = MAX_MODULES,
    ) -> GdbModuleList:
        """Walk the kernel ``modules`` list into a bounded, redacted module list (ADR-0278)."""
        rows, truncated, decode_errors = _module_walk(self, attachment, limit=max_modules)
        modules = [
            _redact_module(
                self,
                GdbModule(
                    name=name,
                    base_address=f"0x{base:x}",
                    symbols_loaded=name in attachment.loaded_modules,
                ),
            )
            for name, base, _ptr in rows
        ]
        return GdbModuleList(modules=modules, truncated=truncated, decode_errors=decode_errors)

    def load_module_symbols(
        self: _ModuleHost,
        attachment: GdbMiAttachment,
        *,
        module: str,
        expected_base: int | None = None,
    ) -> GdbModule:
        """Load one module's symbols at its freshly-read base, identity-checked (ADR-0278)."""
        if not _MODULE_NAME_RE.match(module):
            raise config_error(
                f"module name must be a bare identifier, got {module!r}",
                code="bad_module_name",
                details={"module": module},
            )
        located = _module_locate(self, attachment, module)
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
            return _redact_module(
                self, GdbModule(name=module, base_address=f"0x{base:x}", symbols_loaded=True)
            )
        info = self._module_resolver(attachment, module)
        verified = _verify_identity(self, attachment, module, module_ptr, info)
        _add_symbol_file(self, attachment, module, info.path, base)
        attachment.loaded_modules.add(module)
        return _redact_module(
            self,
            GdbModule(
                name=module,
                base_address=f"0x{base:x}",
                symbols_loaded=True,
                identity_verified=verified,
            ),
        )


def _module_walk(
    host: _ModuleHost, attachment: GdbMiAttachment, *, limit: int
) -> tuple[list[tuple[str, int, int]], bool, int]:
    """Walk ``modules`` from ``modules.next`` to ``&modules``, bounded to ``limit``."""
    offset = _module_eval_required(host, attachment, "&((struct module *)0)->list")
    head = _module_eval_required(host, attachment, "&modules")
    node = _module_eval_required(host, attachment, "modules.next")
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
        name = _module_name(host, attachment, module_ptr)
        base, base_field = _module_base(host, attachment, module_ptr, base_field)
        if name is not None and base is not None:
            rows.append((name, base, module_ptr))
        else:
            decode_errors += 1
        nxt = _module_eval_optional(host, attachment, f"((struct list_head *)0x{node:x})->next")
        if nxt is None or nxt == node:
            break
        node = nxt
    return rows, truncated, decode_errors


def _module_eval_required(host: _ModuleHost, attachment: GdbMiAttachment, expr: str) -> int:
    try:
        value = evaluate_value(host.execute_mi_command(attachment, _eval_command(expr)))
    except CategorizedError as exc:
        raise _module_walk_error(exc) from exc
    addr = _hex_from(value)
    if addr is None:
        raise CategorizedError(
            "gdb/MI returned no parseable module-list pointer",
            category=ErrorCategory.DEBUG_ATTACH_FAILURE,
            details={"code": "module_decode_failed", "expr": expr},
        )
    return addr


def _module_eval_optional(host: _ModuleHost, attachment: GdbMiAttachment, expr: str) -> int | None:
    try:
        value = evaluate_value(host.execute_mi_command(attachment, _eval_command(expr)))
    except CategorizedError as exc:
        _raise_unless_decode_failure(exc)
        return None
    return _hex_from(value)


def _module_name(host: _ModuleHost, attachment: GdbMiAttachment, module_ptr: int) -> str | None:
    try:
        value = evaluate_value(
            host.execute_mi_command(
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
    host: _ModuleHost,
    attachment: GdbMiAttachment,
    module_ptr: int,
    base_field: str,
) -> tuple[int | None, str]:
    if base_field:
        expr = f"((struct module *)0x{module_ptr:x})->{base_field}"
        return _module_eval_optional(host, attachment, expr), base_field
    for candidate in _MODULE_BASE_FIELDS:
        expr = f"((struct module *)0x{module_ptr:x})->{candidate}"
        base = _module_eval_optional(host, attachment, expr)
        if base is not None:
            return base, candidate
    raise CategorizedError(
        "gdb/MI could not read any known module base-address field",
        category=ErrorCategory.DEBUG_ATTACH_FAILURE,
        details={"code": "module_decode_failed", "fields": list(_MODULE_BASE_FIELDS)},
    )


def _module_walk_error(exc: CategorizedError) -> CategorizedError:
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


def _redact_module(host: _ModuleHost, module: GdbModule) -> GdbModule:
    return GdbModule.model_validate(host._redactor().redact_value(module.model_dump(mode="json")))


def _module_locate(
    host: _ModuleHost, attachment: GdbMiAttachment, module: str
) -> tuple[int, int] | None:
    rows, _truncated, _decode_errors = _module_walk(host, attachment, limit=MAX_MODULES)
    for name, base, module_ptr in rows:
        if name == module:
            return base, module_ptr
    return None


def _verify_identity(
    host: _ModuleHost,
    attachment: GdbMiAttachment,
    module: str,
    module_ptr: int,
    info: ModuleDebuginfo,
) -> bool:
    live_src = _module_text_field(host, attachment, module_ptr, "srcversion")
    if live_src is not None and info.srcversion is not None:
        return _identity_match(module, live_src == info.srcversion)
    live_bid = _module_build_id(host, attachment, module_ptr)
    if live_bid is not None and info.build_id is not None:
        return _identity_match(module, live_bid.startswith(info.build_id))
    return False


def _identity_match(module: str, ok: bool) -> bool:
    if ok:
        return True
    raise CategorizedError(
        f"the artifact .ko for {module!r} does not match the running module's identity",
        category=ErrorCategory.DEBUG_ATTACH_FAILURE,
        details={"code": "module_binary_mismatch", "module": module},
    )


def _module_text_field(
    host: _ModuleHost, attachment: GdbMiAttachment, module_ptr: int, field: str
) -> str | None:
    try:
        value = evaluate_value(
            host.execute_mi_command(
                attachment,
                _eval_command(f"((struct module *)0x{module_ptr:x})->{field}"),
            )
        )
    except CategorizedError as exc:
        _raise_unless_decode_failure(exc)
        return None
    match = re.search(r'"([^"]*)"', value) if isinstance(value, str) else None
    return match.group(1) if match is not None and match.group(1) else None


def _module_build_id(host: _ModuleHost, attachment: GdbMiAttachment, module_ptr: int) -> str | None:
    addr = _module_eval_optional(
        host, attachment, f"&((struct module *)0x{module_ptr:x})->build_id"
    )
    if addr is None:
        return None
    try:
        blob = host.read_memory(attachment, address=addr, byte_count=_BUILD_ID_BYTES)
    except CategorizedError as exc:
        _raise_unless_decode_failure(exc)
        return None
    return blob.hex()


def _add_symbol_file(
    host: _ModuleHost,
    attachment: GdbMiAttachment,
    module: str,
    path: Path,
    base: int,
) -> None:
    text = host._mi_path(path)
    if '"' in text:
        raise config_error("staged module path is not safely quotable", code="add_symbol_failed")
    command = f'-interpreter-exec console "add-symbol-file {text} 0x{base:x}"'
    try:
        host.execute_mi_command(attachment, command)
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
