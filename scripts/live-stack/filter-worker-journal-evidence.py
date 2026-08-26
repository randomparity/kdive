#!/usr/bin/env python3
"""Emit only fixed-form provision evidence from JSON worker journal records."""

from __future__ import annotations

import json
import re
import sys
from re import Pattern
from typing import Any

_WORKER = r"local-systemd:kdive-live-worker@[1-8]\.service:[0-9a-f]{32}"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_TIMESTAMP = (
    r"(?:NONE|[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2}))"
)
_LANE = r"[a-z][a-z0-9-]*(?:,[a-z][a-z0-9-]*)*"
_STAGE = (
    r"(?:resolve-arch|materialize-rootfs|prepare-baseline|prepare-overlay|render-domain|"
    r"customize-overlay|prepare-console|define-start)"
)
_PATTERNS: tuple[Pattern[str], ...] = (
    re.compile(rf"worker {_WORKER} accepting dispatch lanes: {_LANE}"),
    re.compile(
        rf"worker {_WORKER} claimed provision job {_UUID} lane=[a-z][a-z0-9-]* "
        rf"attempt=[0-9]+ enqueued_at={_TIMESTAMP} claim_at={_TIMESTAMP} "
        r"queue_delay_s=[0-9]+\.[0-9]{6}"
    ),
    re.compile(
        rf"local-libvirt provision system={_UUID} job=(?:{_UUID}|NONE) "
        rf"stage={_STAGE} event=(?:start|complete)"
    ),
)


def _message(raw: str) -> str | None:
    try:
        payload: Any = json.loads(raw)
    except UnicodeError, json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    message = payload.get("msg")
    return message if isinstance(message, str) else None


def main() -> int:
    """Copy safe fixed-form evidence to stdout; fail when the journal contains none."""
    emitted = 0
    for raw in sys.stdin:
        message = _message(raw)
        if message is None or not any(pattern.fullmatch(message) for pattern in _PATTERNS):
            continue
        print(message)
        emitted += 1
    return 0 if emitted else 1


if __name__ == "__main__":
    raise SystemExit(main())
