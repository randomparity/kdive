"""Temp-then-rename staged file writes for local-libvirt lifecycle artifacts."""

from __future__ import annotations

import contextlib
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory


def write_staged_bytes(dest: Path, data: bytes) -> None:
    """Write ``data`` through a sibling temp file, then atomically replace ``dest``."""
    tmp = dest.with_name(dest.name + ".part")
    try:
        with tmp.open("wb") as handle:
            handle.write(data)
        tmp.replace(dest)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise CategorizedError(
            "failed to write the staged object to the per-Run path",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"op": "stage", "dest": str(dest)},
        ) from exc
