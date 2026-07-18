"""BPF capture-filter hygiene, validation, and post-capture trim (ADR-0384).

The agent-supplied filter is the trailing pcap-filter(7) expression of a tcpdump line. It is passed
to tcpdump as a single argv element (never a shell string), validated compile-only with
``tcpdump -d`` before use, and applied after capture with ``tcpdump -r <src> -w <dst> <expr>``.
Admission does a pure length/printable hygiene check only (no subprocess — the server stays
non-blocking); authoritative validation happens in the worker.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory

MAX_FILTER_LEN = 1024
_SUBPROCESS_TIMEOUT_S = 30


def hygiene_reason(expr: str | None) -> str | None:
    """Cheap admission-time check: return a reason token, or ``None`` if acceptable/absent."""
    if expr is None:
        return None
    if len(expr) > MAX_FILTER_LEN:
        return "too_long"
    if not expr.isprintable():
        return "non_printable"
    return None


def _run(args: list[str], op: str) -> None:
    try:
        proc = subprocess.run(  # noqa: S603 - args is a fixed argv list, never shell-interpreted
            args, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.SubprocessError) as err:
        raise CategorizedError(
            f"{op} failed to run",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": "invalid_filter", "error": type(err).__name__},
        ) from err
    if proc.returncode != 0:
        raise CategorizedError(
            f"{op} rejected the capture filter",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": "invalid_filter", "stderr": proc.stderr.strip()[:500]},
        )


def validate_bpf(expr: str) -> None:
    """Compile-only validation of ``expr`` via ``tcpdump -d`` (no capture); raises on rejection."""
    _run(["tcpdump", "-d", expr], "filter validation")


def trim_pcap(src: Path, dst: Path, expr: str) -> None:
    """Rewrite ``src`` to ``dst`` keeping only packets matching ``expr`` (``tcpdump -r/-w``)."""
    _run(["tcpdump", "-r", str(src), "-w", str(dst), expr], "filter trim")
