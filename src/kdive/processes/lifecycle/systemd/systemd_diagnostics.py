"""Bounded, fail-closed rendering for systemd worker diagnostics."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from itertools import chain

from kdive.processes.lifecycle.systemd.systemd_worker_contract import SlotResult
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
