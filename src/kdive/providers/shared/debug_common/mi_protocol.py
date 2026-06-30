"""gdb/MI record parsing helpers for the local-libvirt debug provider."""

from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel, ConfigDict
from pygdbmi.gdbmiparser import parse_response

# The literal MI prompt terminator gdb emits between command results; not a record.
_MI_PROMPT = "(gdb)"
# Keys pygdbmi may emit on a parsed record; whitelist so an unexpected extra key is dropped
# rather than tripping the extra="forbid" model boundary.
_KNOWN_KEYS = ("type", "message", "payload", "token", "stream")


class _MiModel(BaseModel):
    """Frozen wire shape for parsed gdb/MI records (``extra="forbid"``)."""

    model_config = ConfigDict(extra="forbid")


class MiRecord(_MiModel):
    """One parsed gdb/MI record (gdb manual "GDB/MI Output Syntax")."""

    type: str
    message: str | None = None
    payload: dict[str, Any] | list[Any] | str | None = None
    token: int | None = None
    stream: str | None = None

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> MiRecord:
        return cls(**{key: raw[key] for key in _KNOWN_KEYS if key in raw})

    @staticmethod
    def first_result(records: list[MiRecord]) -> MiRecord | None:
        """The first result-class record, or None."""
        return next((record for record in records if record.type == "result"), None)


def mi_int(value: object) -> int | None:
    return int(value) if isinstance(value, str) and value.lstrip("-").isdigit() else None


def payload_dict(value: object) -> dict[str, Any]:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _payload_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _dict_rows(value: object) -> list[dict[str, Any]]:
    return [row for row in _payload_list(value) if isinstance(row, dict)]


def result_payload_dict(records: list[MiRecord]) -> dict[str, Any]:
    result = MiRecord.first_result(records)
    if result is None:
        return {}
    return payload_dict(result.payload)


def breakpoint_rows(records: list[MiRecord]) -> list[dict[str, Any]]:
    """The breakpoint/watchpoint dicts from a ``-break-list`` result.

    gdb/MI emits ``BreakpointTable={...,body=[bkpt={...},...]}``. Live gdbstub transcripts show
    this pygdbmi flattens each body row to a bare dict (``{number,type,...}`` with no ``"bkpt"``
    wrapper); other pygdbmi versions keep the ``{"bkpt": {...}}`` wrapping (the same divergence
    ``stack_frames`` handles for ``-stack-list-frames``). Accept either: unwrap a dict ``bkpt`` key
    when present, else treat a row carrying a top-level ``number`` as a bare entry; rows that are
    neither (a non-dict ``bkpt``, an unrelated key) are ignored as malformed. Watchpoints appear
    here too, as rows with ``type`` containing ``"watchpoint"`` and the expression in ``what``.
    """
    payload = result_payload_dict(records)
    table = payload_dict(payload.get("BreakpointTable"))
    rows: list[dict[str, Any]] = []
    for row in _dict_rows(table.get("body")):
        entry = row.get("bkpt")
        if isinstance(entry, dict):
            rows.append(entry)
        elif "number" in row:
            rows.append(row)
    return rows


def stack_frames(records: list[MiRecord]) -> list[dict[str, Any]]:
    """The frame dicts from a ``-stack-list-frames`` result.

    gdb/MI emits ``stack=[frame={...},...]``. This pygdbmi flattens that array to bare frame
    dicts (``[{level,addr,func,...}, ...]`` with no ``"frame"`` wrapper) — the shape observed in
    a live gdbstub transcript; other pygdbmi versions keep each row wrapped as ``{"frame": {...}}``
    (the way ``-break-list`` wraps rows as ``{"bkpt": {...}}``). Accept either: unwrap a ``frame``
    key when present, otherwise treat the row itself as the frame.
    """
    rows: list[dict[str, Any]] = []
    for row in _dict_rows(result_payload_dict(records).get("stack")):
        entry = row.get("frame")
        rows.append(entry if isinstance(entry, dict) else row)
    return rows


def disassembly_rows(records: list[MiRecord]) -> list[dict[str, Any]]:
    """The instruction dicts from a ``-data-disassemble`` result.

    gdb/MI mode 0 emits a flat ``asm_insns=[{address,func-name,offset,inst},...]`` list. A
    missing / non-list ``asm_insns`` (malformed output) yields an empty list.
    """
    return _dict_rows(result_payload_dict(records).get("asm_insns"))


def register_names(records: list[MiRecord]) -> list[str]:
    names = result_payload_dict(records).get("register-names")
    return [name for name in _payload_list(names) if isinstance(name, str)]


def register_values_by_number(records: list[MiRecord]) -> dict[str, object]:
    rows = _dict_rows(result_payload_dict(records).get("register-values"))
    by_number: dict[str, object] = {}
    for row in rows:
        number = row.get("number")
        if isinstance(number, str):
            by_number[number] = row.get("value")
    return by_number


def memory_segments(records: list[MiRecord]) -> list[dict[str, Any]]:
    return _dict_rows(result_payload_dict(records).get("memory"))


def evaluate_value(records: list[MiRecord]) -> str | None:
    """The ``value`` string from a ``-data-evaluate-expression`` result, or None if absent."""
    value = result_payload_dict(records).get("value")
    return value if isinstance(value, str) else None


def parse_mi_records(text: str) -> list[MiRecord]:
    """Parse newline-delimited MI output into typed records."""
    records: list[MiRecord] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped == _MI_PROMPT:
            continue
        records.append(MiRecord.from_raw(parse_response(stripped)))
    return records
