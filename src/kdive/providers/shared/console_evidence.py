"""Shared redacted console reads for services and job handlers (ADR-0235, ADR-0306).

``read_redacted_console`` is the single console read and redaction path used by boot evidence,
SSH diagnostics, and run liveness. ``redacted_console_tail`` exposes a bounded best-effort tail
that never masks a consumer's primary failure.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from kdive.providers.shared.runtime_paths import console_log_path, read_console_log
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry

_log = logging.getLogger(__name__)

# Bound the console tail attached to an SSH failure envelope. Larger than the host-side 512-char
# `stderr_tail` (a console line is noisier and the sshd-status signal may sit a few lines back), but
# kept under the worker's 1000-char `_CONTEXT_VALUE_MAX` so the recent tail survives (ADR-0306).
_CONSOLE_TAIL_MAX_CHARS = 800


async def read_redacted_console(system_id: UUID, secret_registry: SecretRegistry) -> bytes | None:
    raw = await asyncio.to_thread(read_console_log, console_log_path(system_id))
    if not raw:
        _log.warning(
            "console log for system %s is empty or unreadable; registering no console artifact",
            system_id,
        )
        return None
    return (
        Redactor(registry=secret_registry)
        .redact_text(raw.decode("utf-8", "replace"))
        .encode("utf-8")
    )


async def redacted_console_tail(
    system_id: UUID,
    secret_registry: SecretRegistry,
    *,
    max_chars: int = _CONSOLE_TAIL_MAX_CHARS,
) -> str | None:
    """Return the last ``max_chars`` characters of the redacted console, or ``None``.

    Empty, absent, worker-unreadable, and failed reads all return ``None`` so console evidence
    cannot mask the consumer's primary operation or failure.
    """
    try:
        redacted = await read_redacted_console(system_id, secret_registry)
    except Exception:
        _log.warning(
            "reading console tail for system %s failed; no console evidence attached",
            system_id,
            exc_info=True,
        )
        return None
    if not redacted:
        return None
    return redacted.decode("utf-8", "replace")[-max_chars:]
