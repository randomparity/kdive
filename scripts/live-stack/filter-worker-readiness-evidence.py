#!/usr/bin/env python3
"""Emit only fixed-form worker readiness component booleans."""

from __future__ import annotations

import json
import sys
from typing import Any

_MAX_BYTES = 4096
_COMPONENTS = (
    "postgres",
    "minio",
    "capture_bootstrap_manifest",
    "capture_recovery",
)


def _read_payload() -> dict[str, Any] | None:
    raw = sys.stdin.buffer.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        return None
    try:
        payload: Any = json.loads(raw)
    except UnicodeError, json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or set(payload) != {"ready", "checks", "version"}:
        return None
    ready = payload["ready"]
    checks = payload["checks"]
    if not isinstance(ready, bool) or not isinstance(checks, dict):
        return None
    if tuple(checks) != _COMPONENTS:
        return None
    if not all(isinstance(checks[name], bool) for name in _COMPONENTS):
        return None
    if ready is not all(checks.values()):
        return None
    return payload


def main() -> int:
    """Print the allowlisted component state; reject every other response shape."""
    payload = _read_payload()
    if payload is None:
        return 1
    checks = payload["checks"]
    values = [f"ready={str(payload['ready']).lower()}"]
    values.extend(f"{name}={str(checks[name]).lower()}" for name in _COMPONENTS)
    print("worker_readiness " + " ".join(values))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
