"""Build sub-phase duration recorder (ADR-0191 G1).

Times each delineated build stage (``BuildPhase``) and emits
``kdive.build.phase.duration{build_phase, provider, outcome}``. Built from the worker meter
and threaded into the shared build orchestrator. The build runs offloaded on a thread
(ADR-0181); ``Histogram.record`` is thread-safe, so the recorder is passed by value and used
inside the thread. ``disabled()`` is the no-op default for un-instrumented runs.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING

from kdive.domain.build_phase import BuildPhase

if TYPE_CHECKING:
    from opentelemetry.metrics import Histogram, Meter

_DURATION_BUCKETS = (0.5, 1.0, 5.0, 15.0, 60.0, 300.0, 900.0, 1800.0, 3600.0)


class BuildPhaseRecorder:
    """Record per-phase build durations (ADR-0191 G1)."""

    def __init__(self, *, meter: Meter) -> None:
        self._enabled = True
        self._duration: Histogram = meter.create_histogram(
            "kdive.build.phase.duration",
            unit="s",
            description="Build sub-phase wall-clock duration, by phase and provider.",
            explicit_bucket_boundaries_advisory=list(_DURATION_BUCKETS),
        )

    @classmethod
    def disabled(cls) -> BuildPhaseRecorder:
        """Return a no-op recorder (no meter) for tests or an un-instrumented run."""
        instance = cls.__new__(cls)
        instance._enabled = False
        return instance

    @contextlib.contextmanager
    def phase(self, build_phase: BuildPhase, provider: str) -> Iterator[None]:
        """Time the wrapped block and record its duration with an ok/error outcome."""
        if not self._enabled:
            yield
            return
        started = time.perf_counter()
        outcome = "ok"
        try:
            yield
        except BaseException:
            outcome = "error"
            raise
        finally:
            self._duration.record(
                time.perf_counter() - started,
                {"build_phase": build_phase.value, "provider": provider, "outcome": outcome},
            )


# Module-level no-op singleton for use as argument defaults (satisfies B008: avoids a function
# call in a default parameter position).
DISABLED_RECORDER: BuildPhaseRecorder = BuildPhaseRecorder.disabled()
