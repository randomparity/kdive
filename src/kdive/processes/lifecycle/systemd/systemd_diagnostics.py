"""Bounded, fail-closed rendering for systemd worker diagnostics."""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path
from typing import Any, Protocol

from kdive.processes.lifecycle.systemd.systemd_worker_contract import LifecycleResponse, SlotResult
from kdive.processes.lifecycle.systemd.systemd_worker_runtime import Deadline
from kdive.processes.lifecycle.systemd.systemd_worker_state import SlotState, StateConflict
from kdive.security.secrets.secret_registry import SecretRegistry

_SLOT_ACQUISITION_BYTES = 320 * 1024
_TOTAL_ACQUISITION_BYTES = 1_310_720
_PROPERTY_ACQUISITION_BYTES = 4096
_SLOT_EMISSION_BYTES = 256 * 1024
_TOTAL_EMISSION_BYTES = 1_048_576
_MAX_REDACTION_VALUES = 32
_MAX_REDACTION_VALUE_BYTES = 4096
_MAX_FORBIDDEN_SOURCE_BYTES = (
    _MAX_REDACTION_VALUES * _MAX_REDACTION_VALUE_BYTES + _SLOT_ACQUISITION_BYTES + 128
)
_TRUNCATION_MARKER = "[diagnostics truncated]\n"
_AGGREGATE_TRUNCATION_MARKER = "[aggregate diagnostics truncated]\n"
_WITHHELD_TEMPLATE = "[diagnostics withheld for slot {slot}]\n"
_URL_USERINFO = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)([^/@\s]+)(@)")
_SCHEMELESS_USERINFO = re.compile(r"(?<![\w/])([^/:@\s]+:[^/@\s]*)(@)(?=[^/\s]+)")
_UNTERMINATED_URL_AUTHORITY = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)([^\s/]*)\Z")
_UNTERMINATED_SCHEMELESS_USERINFO = re.compile(r"(?<![\w/:])([^/:@\s]+:[^/@\s]*)\Z")
_log = logging.getLogger(__name__)

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Za-z0-9_-]*(?:password|passwd|token|api[_-]?key|access[_-]?key|secret|"
    r"credential|database_url)[A-Za-z0-9_-]*)(\s*[=:]\s*)([^\r\n]*)"
)


class _UnsafeDiagnosticText(RuntimeError):
    """Known forbidden text could not be excluded from a post-acquisition rendering."""

    def __init__(
        self,
        forbidden: tuple[str, ...],
        *,
        used: int | None = None,
        aggregate_truncated: bool = False,
    ) -> None:
        super().__init__("diagnostic text could not be rendered safely")
        self.forbidden = forbidden
        self.used = used
        self.aggregate_truncated = aggregate_truncated


@dataclass(slots=True)
class _DiagnosticCapture:
    reports: list[str] = field(default_factory=list)
    results: list[SlotResult] = field(default_factory=list)
    withheld_slots: set[int] = field(default_factory=set)
    forbidden_values: set[str] = field(default_factory=set)
    acquired: int = 0
    emitted: int = 0
    aggregate_truncated: bool = False

    def append(self, report: str) -> None:
        available = max(0, _TOTAL_EMISSION_BYTES - self.emitted)
        bounded = _bounded_text(report, available, truncated=False)
        self.reports.append(bounded)
        self.emitted += len(bounded.encode("utf-8"))


def _bounded_chunks(chunks: Sequence[str], limit: int) -> tuple[str, int, bool]:
    retained = bytearray()
    truncated = False
    for chunk in chunks:
        encoded = chunk.encode("utf-8")
        available = max(0, limit - len(retained))
        retained.extend(encoded[:available])
        if len(encoded) > available:
            truncated = True
            break
    return retained.decode("utf-8", errors="ignore"), len(retained), truncated


def _require_diagnostic_budget(
    state: SlotState, acquisition_budget: int, emission_budget: int
) -> str:
    if state.invocation_id is None:
        raise StateConflict("slot has no exact invocation for diagnostics")
    if acquisition_budget <= 0 or emission_budget <= 0:
        raise StateConflict("slot has no safe diagnostic acquisition budget")
    return state.invocation_id


def _validated_redaction_values(values: tuple[str, ...]) -> tuple[str, ...]:
    if not values or len(values) > _MAX_REDACTION_VALUES:
        raise StateConflict("slot has unsafe diagnostic redaction sources")
    for value in values:
        if not value or len(value.encode("utf-8")) > _MAX_REDACTION_VALUE_BYTES:
            raise StateConflict("slot has unsafe diagnostic redaction sources")
    return values


def _sanitize_diagnostics(
    text: str,
    secret_values: tuple[str, ...],
    *,
    acquisition_truncated: bool,
) -> tuple[str, tuple[str, ...]]:
    registry = SecretRegistry()
    scope = object()
    for value in secret_values:
        registry.register(value, scope=scope)
    registered = tuple(sorted(registry.snapshot(), key=len, reverse=True))
    structural = _structural_secret_values(text, acquisition_truncated=acquisition_truncated)
    forbidden = tuple(dict.fromkeys((*registered, *structural)))
    literals = tuple(sorted(forbidden, key=len, reverse=True))
    sentinels = _mask_sentinels(forbidden)
    sentinel = next(sentinels, None)
    if sentinel is None:
        raise StateConflict("diagnostics have no safe visible redaction sentinel")
    redacted = _render_sanitized_diagnostics(
        text,
        literals,
        sentinel,
        acquisition_truncated=acquisition_truncated,
    )
    if not _contains_forbidden(redacted, forbidden):
        return redacted, forbidden
    # Only ':' changes under destination escaping. Retry that candidate once; any other
    # collision is candidate-independent and must fail closed without another full render.
    if sentinel == ":" and (sentinel := next(sentinels, None)) is not None:
        redacted = _render_sanitized_diagnostics(
            text,
            literals,
            sentinel,
            acquisition_truncated=acquisition_truncated,
        )
        if not _contains_forbidden(redacted, forbidden):
            return redacted, forbidden
    raise StateConflict("diagnostics have no safe visible redaction sentinel")


def _mask_sentinels(forbidden: tuple[str, ...]) -> Iterator[str]:
    occupied = {character for value in forbidden for character in value}
    occupied_bytes = 0
    # Every distinct occupied graphic consumes its UTF-8 width in the bounded source material.
    # Crossing this byte ceiling before finding a gap is therefore impossible by construction.
    codepoints = chain((0x2588,), range(0x21, 0x2588), range(0x2589, 0x110000))
    for codepoint in codepoints:
        candidate = chr(codepoint)
        if unicodedata.category(candidate)[0] not in {"L", "N", "P", "S"}:
            continue
        if candidate not in occupied:
            yield candidate
        else:
            occupied_bytes += len(candidate.encode("utf-8"))
            if occupied_bytes > _MAX_FORBIDDEN_SOURCE_BYTES:
                raise AssertionError("bounded forbidden sources cannot occupy the sentinel space")


def _structural_secret_values(text: str, *, acquisition_truncated: bool) -> tuple[str, ...]:
    values = [match.group(2) for match in _URL_USERINFO.finditer(text)]
    values.extend(match.group(1) for match in _SCHEMELESS_USERINFO.finditer(text))
    values.extend(match.group(3) for match in _SECRET_ASSIGNMENT.finditer(text))
    if acquisition_truncated and (match := _UNTERMINATED_URL_AUTHORITY.search(text)):
        values.append(match.group(2))
    if acquisition_truncated and (match := _UNTERMINATED_SCHEMELESS_USERINFO.search(text)):
        values.append(match.group(1))
    return tuple(value for value in values if value)


def _render_sanitized_diagnostics(
    text: str,
    literals: tuple[str, ...],
    sentinel: str,
    *,
    acquisition_truncated: bool,
) -> str:
    redacted = text
    for value in literals:
        redacted = redacted.replace(value, _mask_bytes(value, sentinel))
    redacted = _URL_USERINFO.sub(
        lambda match: f"{match.group(1)}{_mask_bytes(match.group(2), sentinel)}{match.group(3)}",
        redacted,
    )
    redacted = _SCHEMELESS_USERINFO.sub(
        lambda match: f"{_mask_bytes(match.group(1), sentinel)}{match.group(2)}",
        redacted,
    )
    redacted = _SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{_mask_bytes(match.group(3), sentinel)}",
        redacted,
    )
    if acquisition_truncated:
        redacted = _UNTERMINATED_URL_AUTHORITY.sub(
            lambda match: f"{match.group(1)}{_mask_bytes(match.group(2), sentinel)}",
            redacted,
        )
        redacted = _UNTERMINATED_SCHEMELESS_USERINFO.sub(
            lambda match: _mask_bytes(match.group(1), sentinel),
            redacted,
        )
    return _escape_diagnostic_controls(redacted)


def _mask_bytes(value: str, sentinel: str) -> str:
    value_bytes = len(value.encode("utf-8"))
    sentinel_bytes = len(sentinel.encode("utf-8"))
    return sentinel * ((value_bytes + sentinel_bytes - 1) // sentinel_bytes)


def _contains_forbidden(text: str, forbidden: tuple[str, ...]) -> bool:
    return any(value in text for value in forbidden)


def _escape_diagnostic_controls(text: str) -> str:
    escaped: list[str] = []
    for character in text:
        value = ord(character)
        category = unicodedata.category(character)
        if character == "\n":
            escaped.append(character)
        elif category == "Cc":
            escaped.append(f"\\x{value:02x}")
        elif category in {"Cf", "Zl", "Zp"}:
            width = 4 if value <= 0xFFFF else 8
            escaped.append(f"\\{'u' if width == 4 else 'U'}{value:0{width}x}")
        else:
            escaped.append(character)
    return "".join(escaped).replace("::", "\\x3a\\x3a")


def _bounded_text(text: str, limit: int, *, truncated: bool) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit and not truncated:
        return text
    marker = _TRUNCATION_MARKER.encode()
    if limit <= len(marker):
        return ""
    prefix = encoded[: limit - len(marker) - 1].decode("utf-8", errors="ignore")
    return prefix.rstrip("\n") + "\n" + _TRUNCATION_MARKER


def _aggregate_bounded_text(text: str, limit: int) -> str:
    marker = _AGGREGATE_TRUNCATION_MARKER.encode("utf-8")
    if limit < len(marker):
        return ""
    prefix = text.encode("utf-8")[: limit - len(marker)].decode("utf-8", errors="ignore")
    return prefix + _AGGREGATE_TRUNCATION_MARKER


class _DiagnosticSlotStorage(Protocol):
    slot: int
    unit: str
    root: Path

    def load(self) -> SlotState | None: ...


class _DiagnosticSystemdControl(Protocol):
    def public_properties(self, unit: str, invocation_id: str, deadline: Deadline) -> str: ...

    def journal(
        self, invocation_id: str, byte_limit: int, deadline: Deadline
    ) -> str | Sequence[str]: ...


type _DiagnosticEntry = tuple[_DiagnosticSlotStorage, SlotState | None, bool]
type _BoundaryCall = Callable[..., Any]


class SystemdDiagnostics:
    """Capture and render bounded diagnostics for retained systemd worker slots."""

    def __init__(
        self,
        *,
        stores: Sequence[_DiagnosticSlotStorage],
        runtime: _DiagnosticSystemdControl,
        load_redaction_values: Callable[[Path, int], tuple[str, ...]],
        systemd_call: _BoundaryCall,
        store_call: _BoundaryCall,
        state_failures: tuple[type[Exception], ...],
        acquisition_failures: tuple[type[Exception], ...],
    ) -> None:
        self._stores = tuple(stores)
        self._runtime = runtime
        self._load_redaction_values = load_redaction_values
        self._systemd_call = systemd_call
        self._store_call = store_call
        self._state_failures = state_failures
        self._acquisition_failures = acquisition_failures

    def capture(self, deadline: Deadline) -> LifecycleResponse:
        """Capture bounded diagnostics without mutating lifecycle state."""
        entries = tuple((store, *self._diagnostic_state(store, deadline)) for store in self._stores)
        capture = self._capture_diagnostics(entries, deadline)
        diagnostics = "".join(capture.reports)
        if capture.withheld_slots:
            return LifecycleResponse(
                ok=False,
                code="diagnostics_withheld",
                message="diagnostics withheld for one or more slots",
                retry_action="operator_recovery",
                slots=tuple(capture.results),
                diagnostics=diagnostics,
            )
        return LifecycleResponse(
            ok=True,
            code="ok",
            message="worker diagnostics captured",
            retry_action="none",
            slots=tuple(capture.results),
            diagnostics=diagnostics,
        )

    def _capture_diagnostics(
        self, entries: tuple[_DiagnosticEntry, ...], deadline: Deadline
    ) -> _DiagnosticCapture:
        capture = _DiagnosticCapture()
        for index, (store, state, unsafe_state) in enumerate(entries):
            if unsafe_state:
                capture.withheld_slots.add(store.slot)
                capture.append(_WITHHELD_TEMPLATE.format(slot=store.slot))
                capture.results.append(
                    SlotResult(
                        slot=store.slot,
                        unit=store.unit,
                        code="diagnostics_withheld",
                        message="withheld",
                    )
                )
            elif state is not None:
                has_later = any(
                    later_state is not None or later_unsafe
                    for _, later_state, later_unsafe in entries[index + 1 :]
                )
                capture.results.append(
                    self._capture_diagnostic_slot(
                        store, state, deadline, capture, has_later=has_later
                    )
                )
        return capture

    def _capture_diagnostic_slot(
        self,
        store: _DiagnosticSlotStorage,
        state: SlotState,
        deadline: Deadline,
        capture: _DiagnosticCapture,
        *,
        has_later: bool,
    ) -> SlotResult:
        if capture.aggregate_truncated:
            return _result(state)
        remaining_acquisition = _TOTAL_ACQUISITION_BYTES - capture.acquired
        if remaining_acquisition < _PROPERTY_ACQUISITION_BYTES:
            marker = _AGGREGATE_TRUNCATION_MARKER
            if _contains_forbidden(marker, tuple(capture.forbidden_values)):
                marker = ""
            capture.append(marker)
            capture.aggregate_truncated = True
            return _result(state)
        reservation = min(_SLOT_ACQUISITION_BYTES, remaining_acquisition)
        capture.acquired += reservation
        try:
            report, used, aggregate_truncated, forbidden = self._diagnose_slot(
                store,
                state,
                deadline,
                acquisition_budget=reservation,
                emission_budget=_TOTAL_EMISSION_BYTES - capture.emitted,
                reserve_aggregate=has_later,
            )
        except _UnsafeDiagnosticText as exc:
            if exc.used is not None:
                capture.acquired -= reservation - exc.used
            capture.forbidden_values.update(exc.forbidden)
            capture.withheld_slots.add(store.slot)
            capture.append("")
            capture.aggregate_truncated = exc.aggregate_truncated
            return _result(state, code="diagnostics_withheld")
        except Exception as exc:
            _log.error(
                "unexpected systemd diagnostic capture failure slot=%s cause=%s",
                store.slot,
                type(exc).__name__,
            )
            capture.acquired -= reservation
            capture.withheld_slots.add(store.slot)
            capture.append(_WITHHELD_TEMPLATE.format(slot=store.slot))
            return _result(state, code="diagnostics_withheld")
        capture.acquired -= reservation - used
        capture.forbidden_values.update(forbidden)
        if _contains_forbidden(report, tuple(capture.forbidden_values)):
            capture.withheld_slots.add(store.slot)
            report = ""
        capture.aggregate_truncated = aggregate_truncated
        capture.append(report)
        code = "diagnostics_withheld" if store.slot in capture.withheld_slots else "ok"
        return _result(state, code=code)

    def _diagnostic_state(
        self, store: _DiagnosticSlotStorage, deadline: Deadline
    ) -> tuple[SlotState | None, bool]:
        try:
            return self._store_call(deadline, store.load), False
        except self._state_failures as exc:
            _log.warning(
                "systemd diagnostic state unavailable slot=%s cause=%s",
                store.slot,
                type(exc).__name__,
            )
            return None, True
        except Exception as exc:
            _log.error(
                "unexpected systemd diagnostic state failure slot=%s cause=%s",
                store.slot,
                type(exc).__name__,
            )
            return None, True

    def _diagnose_slot(
        self,
        store: _DiagnosticSlotStorage,
        state: SlotState,
        deadline: Deadline,
        *,
        acquisition_budget: int,
        emission_budget: int,
        reserve_aggregate: bool,
    ) -> tuple[str, int, bool, tuple[str, ...]]:
        invocation_id = _require_diagnostic_budget(state, acquisition_budget, emission_budget)
        secret_values = _validated_redaction_values(
            self._load_redaction_values(store.root, store.slot)
        )
        try:
            return self._diagnose_trusted_slot(
                state,
                deadline,
                secret_values=secret_values,
                invocation_id=invocation_id,
                acquisition_budget=acquisition_budget,
                emission_budget=emission_budget,
                reserve_aggregate=reserve_aggregate,
            )
        except _UnsafeDiagnosticText:
            raise
        except self._acquisition_failures as exc:
            _log.warning(
                "systemd diagnostic acquisition failed slot=%s cause=%s",
                state.slot,
                type(exc).__name__,
            )
            raise _UnsafeDiagnosticText(secret_values) from exc
        except Exception as exc:
            _log.error(
                "unexpected systemd diagnostic acquisition failure slot=%s cause=%s",
                state.slot,
                type(exc).__name__,
            )
            raise _UnsafeDiagnosticText(secret_values) from exc

    def _diagnose_trusted_slot(
        self,
        state: SlotState,
        deadline: Deadline,
        *,
        secret_values: tuple[str, ...],
        invocation_id: str,
        acquisition_budget: int,
        emission_budget: int,
        reserve_aggregate: bool,
    ) -> tuple[str, int, bool, tuple[str, ...]]:
        properties = self._systemd_call(
            deadline,
            self._runtime.public_properties,
            state.unit,
            invocation_id,
            deadline,
        )
        slot_budget = min(_SLOT_ACQUISITION_BYTES, acquisition_budget)
        public_text, _, public_truncated = _bounded_chunks(
            (properties,), _PROPERTY_ACQUISITION_BYTES
        )
        public_bytes = _PROPERTY_ACQUISITION_BYTES
        journal_budget = slot_budget - public_bytes
        journal_text, journal_bytes, journal_truncated = self._diagnostic_journal(
            invocation_id, journal_budget, deadline
        )
        raw = f"=== slot {state.slot} ===\n{public_text}Journal:\n{journal_text}"
        acquisition_truncated = any(
            (public_truncated, journal_truncated, slot_budget < _SLOT_ACQUISITION_BYTES)
        )
        report, forbidden = _sanitize_diagnostics(
            raw, secret_values, acquisition_truncated=acquisition_truncated
        )
        emit_limit = min(_SLOT_EMISSION_BYTES, emission_budget)
        report = _bounded_text(report, emit_limit, truncated=acquisition_truncated)
        aggregate_truncated = reserve_aggregate and (
            len(report.encode("utf-8")) + len(_AGGREGATE_TRUNCATION_MARKER.encode("utf-8"))
            > emission_budget
        )
        if aggregate_truncated:
            report = _aggregate_bounded_text(report, emission_budget)
        if _contains_forbidden(report, forbidden):
            raise _UnsafeDiagnosticText(
                forbidden,
                used=public_bytes + journal_bytes,
                aggregate_truncated=aggregate_truncated,
            )
        return report, public_bytes + journal_bytes, aggregate_truncated, forbidden

    def _diagnostic_journal(
        self, invocation_id: str, byte_limit: int, deadline: Deadline
    ) -> tuple[str, int, bool]:
        if byte_limit <= 0:
            return "", 0, True
        chunks = self._systemd_call(
            deadline,
            self._runtime.journal,
            invocation_id,
            byte_limit,
            deadline,
        )
        source = (chunks,) if isinstance(chunks, str) else chunks
        text, used, truncated = _bounded_chunks(source, byte_limit)
        return text, used, truncated or used >= byte_limit


def _result(state: SlotState, *, code: str = "ok") -> SlotResult:
    return SlotResult(slot=state.slot, unit=state.unit, code=code, message=state.phase.value)
