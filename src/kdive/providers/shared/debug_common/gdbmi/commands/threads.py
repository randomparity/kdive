"""Thread (vCPU) listing and selection for :mod:`kdive.providers.shared.debug_common.gdbmi`.

QEMU's gdbstub exposes each vCPU as a gdb thread. The selection is held on the attachment and
passed to gdb as ``--thread`` by the register and stack families, so a stop that moves gdb's own
current thread does not change what a contributor reads (ADR-0819).
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.debug import GdbMiAttachment, GdbThread, GdbThreadList
from kdive.providers.shared.debug_common.gdbmi._errors import config_error
from kdive.providers.shared.debug_common.gdbmi.core.mi_protocol import (
    MiRecord,
    result_payload_dict,
    thread_rows,
)
from kdive.security.secrets.redaction import Redactor

MAX_THREADS = 128
_INVALID_THREAD_RE = re.compile(r"invalid thread id", re.IGNORECASE)


def thread_option(attachment: GdbMiAttachment) -> str:
    """The ``--thread N`` MI global option for the selected thread, with a leading space."""
    return f" --thread {attachment.selected_thread}" if attachment.selected_thread else ""


def stale_thread_error(
    exc: CategorizedError, attachment: GdbMiAttachment
) -> CategorizedError | None:
    """``unknown_thread`` for a gdb ``Invalid thread id`` answer to a selected-thread command."""
    payload = exc.details.get("payload")
    msg = payload.get("msg") if isinstance(payload, dict) else None
    if (
        attachment.selected_thread is None
        or exc.category is not ErrorCategory.DEBUG_ATTACH_FAILURE
        or not isinstance(msg, str)
        or not _INVALID_THREAD_RE.search(msg)
    ):
        return None
    return _unknown_thread(attachment.selected_thread)


def _unknown_thread(thread_id: object) -> CategorizedError:
    return CategorizedError(
        f"gdb lists no thread {thread_id}; list_threads shows the current ones",
        category=ErrorCategory.DEBUG_ATTACH_FAILURE,
        details={"code": "unknown_thread", "thread_id": thread_id},
    )


def _malformed() -> CategorizedError:
    return CategorizedError(
        "gdb/MI returned a thread list without a thread list or a numeric thread id",
        category=ErrorCategory.DEBUG_ATTACH_FAILURE,
        details={"code": "malformed_thread_info"},
    )


class _ThreadHost(Protocol):
    def execute_mi_command(self, attachment: GdbMiAttachment, command: str) -> list[MiRecord]: ...

    def _redactor(self) -> Redactor: ...


class GdbMiThreadCommands:
    """Thread-listing and selection GDB/MI commands (ADR-0819)."""

    def list_threads(self: _ThreadHost, attachment: GdbMiAttachment) -> GdbThreadList:
        records = self.execute_mi_command(attachment, "-thread-info")
        rows = _checked_rows(records)
        if not rows:
            raise CategorizedError(
                "gdb/MI reported no threads",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"code": "no_threads"},
            )
        current = result_payload_dict(records).get("current-thread-id")
        listing = GdbThreadList(
            threads=[_thread_from(row) for row in rows[:MAX_THREADS]],
            truncated=len(rows) > MAX_THREADS,
            current_thread_id=current if isinstance(current, str) and current.isdigit() else None,
            selected_thread_id=attachment.selected_thread,
        )
        redacted = self._redactor().redact_value(listing.model_dump(mode="json", by_alias=True))
        return GdbThreadList.model_validate(redacted)

    def select_thread(
        self: _ThreadHost, attachment: GdbMiAttachment, *, thread_id: int
    ) -> GdbThread:
        if not isinstance(thread_id, int) or isinstance(thread_id, bool) or thread_id < 1:
            raise config_error(
                f"thread_id must be a positive integer, got {thread_id!r}",
                code="bad_thread_id",
                details={"thread_id": thread_id},
            )
        rows = _checked_rows(self.execute_mi_command(attachment, f"-thread-info {thread_id}"))
        row = next((r for r in rows if r["id"] == str(thread_id)), None)
        if row is None:
            raise _unknown_thread(thread_id)
        attachment.selected_thread = str(thread_id)
        redacted = self._redactor().redact_value(
            _thread_from(row).model_dump(mode="json", by_alias=True)
        )
        return GdbThread.model_validate(redacted)


def _checked_rows(records: list[MiRecord]) -> list[dict[str, Any]]:
    rows = thread_rows(records)
    if rows is None:
        raise _malformed()
    for row in rows:
        thread = row.get("id")
        if not isinstance(thread, str) or not thread.isdigit():
            raise _malformed()
    return rows


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _thread_from(row: dict[str, Any]) -> GdbThread:
    frame = row.get("frame")
    frame = frame if isinstance(frame, dict) else {}
    return GdbThread(
        id=row["id"],
        target_id=_text(row.get("target-id")),
        details=_text(row.get("details")),
        name=_text(row.get("name")),
        state=_text(row.get("state")),
        func=_text(frame.get("func")),
        addr=_text(frame.get("addr")),
    )
