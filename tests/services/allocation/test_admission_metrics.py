"""Admission decision classification + metric emission (ADR-0190 group D)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from kdive.domain.capacity.state import AllocationState
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle import Allocation
from kdive.services.allocation.admission.core import (
    AFFINITY_DENIAL_REASON,
    BUDGET_DENIAL_REASON,
    AdmissionOutcome,
)
from kdive.services.allocation.admission.metrics import (
    AdmissionDecision,
    AdmissionMetrics,
    _AdmissionReason,
    classify,
)


def _alloc(state: AllocationState) -> Allocation:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Allocation(
        id=uuid4(),
        created_at=now,
        updated_at=now,
        principal="alice",
        agent_session=None,
        project="proj",
        resource_id=None,
        state=state,
        lease_expiry=None,
        requested_vcpus=1,
        requested_memory_gb=1,
        requested_disk_gb=1,
        shape="vm",
        pcie_claim=[],
    )


def _denial(category: ErrorCategory, reason: str | None = None) -> AdmissionOutcome:
    return AdmissionOutcome(granted=False, allocation=None, category=category, reason=reason)


def test_classify_grant_and_enqueue_distinguished_by_allocation_state() -> None:
    granted = AdmissionOutcome(granted=True, allocation=_alloc(AllocationState.GRANTED))
    assert classify(granted) == (AdmissionDecision.GRANTED, _AdmissionReason.NONE)
    # An enqueue is a success outcome carrying a REQUESTED allocation, not a real grant.
    enqueued = AdmissionOutcome(granted=True, allocation=_alloc(AllocationState.REQUESTED))
    assert classify(enqueued) == (AdmissionDecision.QUEUED, _AdmissionReason.NONE)


def test_classify_denials_to_bounded_reasons() -> None:
    assert classify(_denial(ErrorCategory.QUOTA_EXCEEDED)) == (
        AdmissionDecision.REJECTED,
        _AdmissionReason.QUOTA,
    )
    assert classify(_denial(ErrorCategory.ALLOCATION_DENIED, BUDGET_DENIAL_REASON)) == (
        AdmissionDecision.REJECTED,
        _AdmissionReason.BUDGET,
    )
    assert classify(_denial(ErrorCategory.ALLOCATION_DENIED, AFFINITY_DENIAL_REASON)) == (
        AdmissionDecision.REJECTED,
        _AdmissionReason.AFFINITY,
    )
    assert classify(_denial(ErrorCategory.ALLOCATION_DENIED, "at_capacity")) == (
        AdmissionDecision.REJECTED,
        _AdmissionReason.CAPACITY,
    )
    # PCIe-busy is the only ALLOCATION_DENIED with no reason string → pcie by elimination.
    assert classify(_denial(ErrorCategory.ALLOCATION_DENIED, None)) == (
        AdmissionDecision.REJECTED,
        _AdmissionReason.PCIE,
    )
    # Both input-validation and PCIe-grammar denials raise CONFIGURATION_ERROR → configuration.
    assert classify(_denial(ErrorCategory.CONFIGURATION_ERROR)) == (
        AdmissionDecision.REJECTED,
        _AdmissionReason.CONFIGURATION,
    )


def test_classify_unmatched_denial_is_unknown() -> None:
    # A category with no admission mapping must not silently mislabel.
    assert classify(_denial(ErrorCategory.NOT_FOUND)) == (
        AdmissionDecision.REJECTED,
        _AdmissionReason.UNKNOWN,
    )


def _points(reader: InMemoryMetricReader, name: str) -> dict[tuple[tuple[str, str], ...], float]:
    data = reader.get_metrics_data()
    assert data is not None
    points: dict[tuple[tuple[str, str], ...], float] = {}
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name != name:
                    continue
                for point in metric.data.data_points:
                    value = getattr(point, "value", None)
                    if value is None:
                        continue
                    attrs = point.attributes or {}
                    key = tuple(sorted((str(k), str(v)) for k, v in attrs.items()))
                    points[key] = value
    return points


def _histogram_count(reader: InMemoryMetricReader, name: str) -> int:
    data = reader.get_metrics_data()
    assert data is not None
    total = 0
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name != name:
                    continue
                for point in metric.data.data_points:
                    total += getattr(point, "count", 0)
    return total


def _metrics() -> tuple[AdmissionMetrics, InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader]).get_meter("test")
    return AdmissionMetrics(meter=meter), reader


def _metric_names(reader: InMemoryMetricReader) -> set[str]:
    data = reader.get_metrics_data()
    if data is None:
        return set()
    return {
        metric.name
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for metric in sm.metrics
    }


def test_record_decision_increments_counter() -> None:
    metrics, reader = _metrics()
    metrics.record_decision(_denial(ErrorCategory.QUOTA_EXCEEDED))
    points = _points(reader, "kdive.allocation.admission")
    assert points[(("outcome", "rejected"), ("reason", "quota"))] == 1


def test_instrument_names_are_the_published_contract() -> None:
    # Dashboards/alerts query these exact instrument names; a rename silently breaks them.
    metrics, reader = _metrics()
    metrics.record_decision(_denial(ErrorCategory.QUOTA_EXCEEDED))
    metrics.record_promotion(1.0)
    names = _metric_names(reader)
    assert "kdive.allocation.admission" in names
    assert "kdive.allocation.wait" in names


def test_record_promotion_emits_grant_and_wait() -> None:
    metrics, reader = _metrics()
    metrics.record_promotion(12.5)
    points = _points(reader, "kdive.allocation.admission")
    assert points[(("outcome", "granted"), ("reason", "none"))] == 1
    assert _histogram_count(reader, "kdive.allocation.wait") == 1


def test_record_promotion_records_zero_wait() -> None:
    # A synchronous grant waits ~0s; a zero wait is still a real sample and must be recorded.
    metrics, reader = _metrics()
    metrics.record_promotion(0.0)
    assert _histogram_count(reader, "kdive.allocation.wait") == 1


def test_record_promotion_skips_negative_wait() -> None:
    # A negative wait is unobservable nonsense (clock skew); it must never reach the histogram.
    metrics, reader = _metrics()
    metrics.record_promotion(-1.0)
    assert _histogram_count(reader, "kdive.allocation.wait") == 0


def test_record_queue_timeout_increments_rejections() -> None:
    metrics, reader = _metrics()
    metrics.record_queue_timeout(3)
    points = _points(reader, "kdive.allocation.admission")
    assert points[(("outcome", "rejected"), ("reason", "queue_timeout"))] == 3


def test_record_queue_timeout_defaults_to_one() -> None:
    metrics, reader = _metrics()
    metrics.record_queue_timeout()
    points = _points(reader, "kdive.allocation.admission")
    assert points[(("outcome", "rejected"), ("reason", "queue_timeout"))] == 1


def test_record_queue_timeout_zero_count_emits_nothing() -> None:
    # A zero reap count is a no-op; it must not emit a spurious zero-value rejection point.
    metrics, reader = _metrics()
    metrics.record_queue_timeout(0)
    assert "kdive.allocation.admission" not in _metric_names(reader)


def test_disabled_metrics_are_noop() -> None:
    metrics = AdmissionMetrics.disabled()
    metrics.record_decision(_denial(ErrorCategory.QUOTA_EXCEEDED))  # must not raise
    metrics.record_promotion(1.0)
    metrics.record_queue_timeout()
