"""Finalized console-bytes counter (ADR-0191 H2).

Aggregate total console bytes finalized — no per-System label (ADR-0090 §4). ``outcome``
splits a content-bearing finalize (``success``) from an empty one (``empty``) so a 0-byte
console (the #594 failure shape) stays visible. Remote-libvirt console scope (ADR-0191 H2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opentelemetry.metrics import Counter, Meter


class ConsoleTelemetry:
    """Count finalized console bytes (ADR-0191 H2)."""

    def __init__(self, *, meter: Meter) -> None:
        self._enabled = True
        self._bytes: Counter = meter.create_counter(
            "kdive.console.bytes",
            unit="By",
            description="Console bytes finalized (remote-libvirt), by content outcome.",
        )

    @classmethod
    def disabled(cls) -> ConsoleTelemetry:
        """Return a no-op telemetry for tests or an un-instrumented run."""
        instance = cls.__new__(cls)
        instance._enabled = False
        return instance

    def record(self, byte_len: int) -> None:
        """Add ``byte_len`` under ``success``, or mark an empty finalize under ``empty``."""
        if not self._enabled:
            return
        if byte_len > 0:
            self._bytes.add(byte_len, {"outcome": "success"})
        else:
            self._bytes.add(0, {"outcome": "empty"})
