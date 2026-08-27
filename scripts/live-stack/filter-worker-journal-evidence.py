#!/usr/bin/env python3
"""Emit only fixed-form provision evidence from JSON worker journal records."""

from __future__ import annotations

import json
import re
import sys
from re import Pattern
from typing import Any

_MAX_RECORD_BYTES = 4096
_MAX_TOTAL_BYTES = 256 * 1024
_MAX_RECORDS = 400
_MAX_OUTPUT_RECORD_BYTES = 1024
_MAX_OUTPUT_BYTES = _MAX_RECORDS * _MAX_OUTPUT_RECORD_BYTES

_WORKER = r"local-systemd:kdive-live-worker@[1-8]\.service:[0-9a-f]{32}"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_TIMESTAMP = (
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)
_LANE = r"[a-z][a-z0-9-]{0,62}(?:,[a-z][a-z0-9-]{0,62}){0,7}"
_STAGE = (
    r"(?:resolve-arch|snapshot-pre-existing|materialize-rootfs|prepare-baseline|"
    r"prepare-overlay|render-domain|customize-overlay|prepare-console|define-start)"
)
_PATTERNS: tuple[Pattern[str], ...] = (
    re.compile(rf"worker {_WORKER} accepting dispatch lanes: {_LANE}"),
    re.compile(
        rf"worker {_WORKER} claimed provision job {_UUID} lane=[a-z][a-z0-9-]{{0,62}} "
        rf"attempt=[0-9]{{1,10}} enqueued_at={_TIMESTAMP} claim_at={_TIMESTAMP} "
        r"queue_delay_s=[0-9]{1,12}\.[0-9]{6}"
    ),
    re.compile(
        rf"local-libvirt provision system={_UUID} job=(?:{_UUID}|NONE) "
        rf"stage={_STAGE} event=(?:start|complete)"
    ),
)


def _message(raw: bytes) -> str | None:
    try:
        payload: Any = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):  # fmt: skip
        return None
    if not isinstance(payload, dict):
        return None
    message = payload.get("msg")
    return message if isinstance(message, str) else None


def main() -> int:
    """Emit bounded safe evidence atomically; fail when input or output exceeds a bound."""
    records = 0
    total_bytes = 0
    output_bytes = 0
    output: list[bytes] = []
    while raw := sys.stdin.buffer.readline(_MAX_RECORD_BYTES + 1):
        records += 1
        total_bytes += len(raw)
        if records > _MAX_RECORDS or len(raw) > _MAX_RECORD_BYTES or total_bytes > _MAX_TOTAL_BYTES:
            return 1
        message = _message(raw)
        if message is None or not any(pattern.fullmatch(message) for pattern in _PATTERNS):
            continue
        encoded = f"{message}\n".encode()
        output_bytes += len(encoded)
        if len(encoded) > _MAX_OUTPUT_RECORD_BYTES or output_bytes > _MAX_OUTPUT_BYTES:
            return 1
        output.append(encoded)
    if not output:
        return 1
    sys.stdout.buffer.write(b"".join(output))
    return 0


if __name__ == "__main__":
    try:
        status = main()
    except Exception:
        status = 1
    raise SystemExit(status)
