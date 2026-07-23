"""Worker handler for the `watch_for_crash` job (ADR-0367, #984).

Watches a ready local-libvirt guest's serial console for the boot-readiness crash signature
(``first_crash_signature``) until a clamped wall-clock deadline, returning on the first hit with a
redacted matched slice, the matched signature, and elapsed-to-signal. The reproducer loop that
provokes the crash stays the agent's own code over root SSH — this job supplies only what SSH
cannot: catching the crash on the out-of-band console after the panic drops SSH.

The console is polled lock-free: each poll snapshots the log and scans the suffix past the
watch's start offset (``mark``); the local serial log only grows while the guest is up
(``append="off"`` truncates only on power-cycle, ADR-0258), so concurrent growth between polls is
harmless. A power-cycle *during* a watch (a ``panic=N`` auto-reboot, or ``control.power reset``)
truncates the log; the watch resets ``mark`` to 0 when it observes the shrink (`len(body) <
mark`). If the new boot regrows past the old ``mark`` within one poll interval the shrink is
missed and a crash in the new boot's head could be skipped — a narrow gap that mainly affects
auto-rebooting guests; the tool's target debug guests halt on panic (``panic=0``), preserving the
console. A boot-identity check (``dev:ino:mtime``) is deliberately **not** used: serial-log mtime
advances on every append (cf. ``console_rotate._boot_id``), so it would false-reset ``mark``
mid-boot and resurface a pre-watch panic as a false ``fired``.

Because ``mark`` is snapshotted at worker pickup, a panic that landed before it (queue latency,
or an at-least-once retry) is outside the scanned suffix and returns ``not_fired``. That
window is covered without a provider-crossing liveness probe: the agent driving the reproducer
over SSH already holds the authoritative liveness signal — its SSH channel drops the instant the
kernel panics — so a ``not_fired`` verdict paired with a dropped SSH loop means "read the full
console" (documented on the tool and in the race-debugging guide).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from psycopg import AsyncConnection

from kdive.db.repositories import SYSTEMS
from kdive.domain.capacity.state import SystemState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.crash_signatures import first_crash_signature
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.context import context_from_job
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import WatchForCrashPayload, load_payload
from kdive.jobs.provider_context import set_provider_kind
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.shared.runtime_paths import console_log_path, read_console_log
from kdive.security import audit
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.serialization import JsonValue

_log = logging.getLogger(__name__)

POLL_INTERVAL_S = 1.0
"""Seconds between console reads. The fired/not-fired verdict is accurate to within one poll."""
CONTEXT_LINES = 3
"""Lines of surrounding console kept on each side of the matched line in the returned slice."""
MATCHED_MAX_BYTES = 4096
"""Byte cap applied to the matched slice **after** redaction, so a boundary-straddling secret is
masked before it can be cut and the returned field still stays compact."""

Outcome = Literal["fired", "not_fired"]


@dataclass(frozen=True, slots=True)
class WatchVerdict:
    """The console-watch outcome, serialized inline into the job's ``result_ref`` (ADR-0164)."""

    outcome: Outcome
    signature: str | None
    matched: str | None
    elapsed_s: float
    observed_at: str

    @property
    def fired(self) -> bool:
        """The agent-facing boolean, derived from ``outcome`` (not stored, so it cannot drift)."""
        return self.outcome == "fired"

    def to_json(self) -> str:
        doc: dict[str, JsonValue] = {
            "outcome": self.outcome,
            "fired": self.fired,
            "elapsed_s": self.elapsed_s,
            "observed_at": self.observed_at,
        }
        if self.signature is not None:
            doc["signature"] = self.signature
        if self.matched is not None:
            doc["matched"] = self.matched
        return json.dumps(doc, separators=(",", ":"))


def _context_window(text: str, start_offset: int, context_lines: int) -> str:
    """Return the line at ``start_offset`` plus ``context_lines`` on each side (uncapped)."""
    line_index = text.count("\n", 0, start_offset)
    lines = text.split("\n")
    lo = max(0, line_index - context_lines)
    hi = min(len(lines), line_index + context_lines + 1)
    return "\n".join(lines[lo:hi])


def _cap_bytes(value: str, max_bytes: int) -> str:
    """Truncate ``value`` to at most ``max_bytes`` UTF-8 bytes on a codepoint boundary."""
    encoded = value.encode("utf-8")
    if len(encoded) > max_bytes:
        return encoded[:max_bytes].decode("utf-8", "ignore")
    return value


def _redacted_slice(
    text: str, start_offset: int, redact: Callable[[str], str], context_lines: int, max_bytes: int
) -> str:
    """Build the returned matched slice: context window → redact → byte cap.

    Redaction runs on the **full** window before the byte cap, so a registered secret straddling
    ``max_bytes`` is masked before it can be cut (a cap-then-redact order would emit the secret's
    surviving prefix verbatim). The cap then bounds the redacted result.
    """
    window = _context_window(text, start_offset, context_lines)
    return _cap_bytes(redact(window), max_bytes)


async def watch_console_for_crash(
    read_console: Callable[[], Awaitable[bytes]],
    sleep: Callable[[float], Awaitable[None]],
    clock: Callable[[], float],
    redact: Callable[[str], str],
    now: Callable[[], str],
    *,
    mark: int,
    deadline_s: float,
    poll_interval: float,
    context_lines: int,
    max_bytes: int,
) -> WatchVerdict:
    """Poll the console suffix past ``mark`` for a crash signature until the first hit or deadline.

    Pure and fully injectable: ``read_console`` yields the current console bytes, ``clock`` is a
    monotonic time source, ``redact`` masks the returned slice, and ``now`` stamps
    ``observed_at``. Returns ``fired`` on the first ``first_crash_signature`` match past ``mark``,
    else ``not_fired`` at the deadline. A read shorter than ``mark`` (a power-cycle truncation)
    resets ``mark`` to 0.
    """
    start = clock()
    current_mark = mark
    while True:
        body = await read_console()
        if len(body) < current_mark:
            _log.info("console truncated below watch mark (%d < %d); rescanning", len(body), mark)
            current_mark = 0
        text = body[current_mark:].decode("utf-8", "replace")
        match = first_crash_signature(text)
        elapsed = clock() - start
        if match is not None:
            slice_text = _redacted_slice(text, match.start(), redact, context_lines, max_bytes)
            return WatchVerdict("fired", match.group(0), slice_text, elapsed, now())
        if elapsed >= deadline_s:
            return WatchVerdict("not_fired", None, None, elapsed, now())
        await sleep(min(poll_interval, deadline_s - elapsed))


def _observed_at() -> str:
    return datetime.now(UTC).isoformat()


async def watch_for_crash_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
) -> str | None:
    """Watch a ready local-libvirt System's console for a crash signature; return the JSON verdict.

    Both outcomes (fired / not_fired) are successful runs — only an inability to run raises. A
    rising failure rate for ``kind=watch_for_crash`` in the worker's per-kind job telemetry
    surfaces a silently-broken console read.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` ``reason="system_not_ready"`` when the System is
            not ready, or ``reason="crash_watch_unsupported"`` for a provider that does not
            advertise ``supports_crash_watch``.
    """
    payload = load_payload(job, WatchForCrashPayload)
    system_id = UUID(payload.system_id)
    system = await SYSTEMS.get(conn, system_id)
    if system is None or system.state is not SystemState.READY:
        raise CategorizedError(
            "system is not ready; cannot watch for a crash signature",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": "system_not_ready"},
        )
    binding = await resolver.binding_for_system(conn, system_id)
    set_provider_kind(binding.kind.value)
    if not binding.runtime.support.supports_crash_watch:
        raise CategorizedError(
            "provider does not support out-of-band crash-watch",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": "crash_watch_unsupported", "provider_kind": binding.kind.value},
        )
    log_path = console_log_path(system_id)
    mark = len(await asyncio.to_thread(read_console_log, log_path))
    redactor = Redactor(registry=secret_registry)
    verdict = await watch_console_for_crash(
        lambda: asyncio.to_thread(read_console_log, log_path),
        asyncio.sleep,
        time.monotonic,
        redactor.redact_text,
        _observed_at,
        mark=mark,
        # deadline_s is already clamped to WATCH_MAX_DEADLINE_S by WatchForCrashPayload's
        # validator (the worker-side backstop), so the payload value is authoritative here.
        deadline_s=payload.deadline_s,
        poll_interval=POLL_INTERVAL_S,
        context_lines=CONTEXT_LINES,
        max_bytes=MATCHED_MAX_BYTES,
    )
    await _record_audit(conn, job, system.project, system_id, verdict)
    return verdict.to_json()


async def _record_audit(
    conn: AsyncConnection, job: Job, project: str, system_id: UUID, verdict: WatchVerdict
) -> None:
    async with conn.transaction():
        await audit.record(
            conn,
            context_from_job(job, project),
            audit.AuditEvent(
                tool="control.watch_for_crash",
                object_kind="systems",
                object_id=system_id,
                transition=f"watch_for_crash:{verdict.outcome}",
                args={"system_id": str(system_id), "outcome": verdict.outcome},
                project=project,
            ),
        )


def register_handlers(
    registry: HandlerRegistry,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
) -> None:
    """Bind the ``watch_for_crash`` job handler with its provider + redaction deps."""
    registry.register(
        JobKind.WATCH_FOR_CRASH,
        lambda conn, job: watch_for_crash_handler(
            conn, job, resolver=resolver, secret_registry=secret_registry
        ),
    )
