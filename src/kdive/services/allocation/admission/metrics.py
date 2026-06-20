"""Admission decision metrics (ADR-0190 group D).

A small emitter over the admission decision counter (``kdive.allocation.admission``, labels
``outcome`` ∈ {granted, rejected, queued} and ``reason`` ∈ :class:`_AdmissionReason`) and the
request→grant wait histogram (``kdive.allocation.wait``). The synchronous ``admit()`` boundary
records via :meth:`AdmissionMetrics.record_decision` (classifying the returned
:class:`AdmissionOutcome`); the reconciler promotion sweep records a grant + wait via
:meth:`record_promotion`, and the queue-timeout reaper a rejection via
:meth:`record_queue_timeout`.

``classify`` keys on the **full** outcome shape, not the category alone: an enqueued request is
a *success* outcome carrying a ``REQUESTED`` allocation, and ``CONFIGURATION_ERROR`` is shared
by input validation and PCIe grammar. Both `outcome` and `reason` are bounded enums (ADR-0090
§4); no per-object / per-tenant label travels.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import TYPE_CHECKING

from kdive.domain.capacity.state import AllocationState
from kdive.domain.errors import ErrorCategory
from kdive.services.allocation.admission.core import (
    AFFINITY_DENIAL_REASON,
    BUDGET_DENIAL_REASON,
    AdmissionOutcome,
)

if TYPE_CHECKING:
    from opentelemetry.metrics import Counter, Histogram, Meter

_log = logging.getLogger(__name__)

#: The host-cap denial's reason string (a literal in ``admission.core._host_cap_check``).
_AT_CAPACITY_REASON = "at_capacity"

#: Histogram bucket bounds (seconds) for request→grant wait — a synchronous grant is ~0, a
#: queued request may wait seconds to many minutes for a freed slot.
_WAIT_BUCKETS = (0.5, 1.0, 5.0, 15.0, 60.0, 300.0, 900.0, 1800.0, 3600.0)


class AdmissionDecision(StrEnum):
    """The bounded ``outcome`` label values admission adds (ADR-0190 D)."""

    GRANTED = "granted"
    REJECTED = "rejected"
    QUEUED = "queued"


class _AdmissionReason(StrEnum):
    """The bounded ``reason`` label values (ADR-0190 D)."""

    NONE = "none"
    QUOTA = "quota"
    BUDGET = "budget"
    CAPACITY = "capacity"
    AFFINITY = "affinity"
    PCIE = "pcie"
    CONFIGURATION = "configuration"
    QUEUE_TIMEOUT = "queue_timeout"
    UNKNOWN = "unknown"


def classify(outcome: AdmissionOutcome) -> tuple[AdmissionDecision, _AdmissionReason]:
    """Map an :class:`AdmissionOutcome` to its ``(outcome, reason)`` label pair.

    A queueable denial that ``on_capacity=queue`` enqueues returns ``granted=True`` carrying a
    ``REQUESTED`` allocation, so ``queued`` vs ``granted`` is read from the allocation's state,
    never from ``granted`` alone. PCIe-busy is the only ``ALLOCATION_DENIED`` with no reason
    string, so it is matched by elimination after the reason-bearing shapes; both validation
    and PCIe-grammar denials fold into ``configuration``. An unmatched shape → ``unknown`` with
    a warning so a new denial shape is visibly anomalous rather than silently mislabeled.
    """
    if outcome.granted:
        if outcome.allocation is not None and outcome.allocation.state is AllocationState.REQUESTED:
            return AdmissionDecision.QUEUED, _AdmissionReason.NONE
        return AdmissionDecision.GRANTED, _AdmissionReason.NONE
    reason = _denial_reason(outcome)
    return AdmissionDecision.REJECTED, reason


def _denial_reason(outcome: AdmissionOutcome) -> _AdmissionReason:
    if outcome.category is ErrorCategory.QUOTA_EXCEEDED:
        return _AdmissionReason.QUOTA
    if outcome.category is ErrorCategory.CONFIGURATION_ERROR:
        return _AdmissionReason.CONFIGURATION
    if outcome.category is ErrorCategory.ALLOCATION_DENIED:
        if outcome.reason == BUDGET_DENIAL_REASON:
            return _AdmissionReason.BUDGET
        if outcome.reason == AFFINITY_DENIAL_REASON:
            return _AdmissionReason.AFFINITY
        if outcome.reason == _AT_CAPACITY_REASON:
            return _AdmissionReason.CAPACITY
        if outcome.reason is None:
            return _AdmissionReason.PCIE  # PCIe-busy is the only reasonless ALLOCATION_DENIED
    _log.warning(
        "admission metrics: unclassified denial category=%s reason=%s",
        outcome.category,
        outcome.reason,
    )
    return _AdmissionReason.UNKNOWN


class AdmissionMetrics:
    """Emit the admission decision counter + wait histogram (ADR-0190 D).

    Args:
        meter: The meter (the facade's server/reconciler ``MeterProvider``) instruments are
            made on.
    """

    def __init__(self, *, meter: Meter) -> None:
        self._enabled = True
        self._decisions: Counter = meter.create_counter(
            "kdive.allocation.admission",
            unit="1",
            description="Allocation admission decisions, by outcome and reason.",
        )
        self._wait: Histogram = meter.create_histogram(
            "kdive.allocation.wait",
            unit="s",
            description="Request→grant wait for a promoted allocation.",
            explicit_bucket_boundaries_advisory=list(_WAIT_BUCKETS),
        )

    @classmethod
    def disabled(cls) -> AdmissionMetrics:
        """Return a no-op emitter (no meter) for tests or an un-instrumented run."""
        instance = cls.__new__(cls)
        instance._enabled = False
        return instance

    def record_decision(self, outcome: AdmissionOutcome) -> None:
        """Record one synchronous admit() decision, classified from ``outcome``."""
        if not self._enabled:
            return
        decision, reason = classify(outcome)
        self._decisions.add(1, {"outcome": decision.value, "reason": reason.value})

    def record_promotion(self, wait_seconds: float) -> None:
        """Record a reconciler promotion: a grant decision + the request→grant wait."""
        if not self._enabled:
            return
        self._decisions.add(
            1, {"outcome": AdmissionDecision.GRANTED.value, "reason": _AdmissionReason.NONE.value}
        )
        if wait_seconds >= 0.0:
            self._wait.record(wait_seconds)

    def record_queue_timeout(self, count: int = 1) -> None:
        """Record ``count`` queued requests reaped as permanently unplaceable (queue_timeout)."""
        if self._enabled and count > 0:
            self._decisions.add(
                count,
                {
                    "outcome": AdmissionDecision.REJECTED.value,
                    "reason": _AdmissionReason.QUEUE_TIMEOUT.value,
                },
            )
