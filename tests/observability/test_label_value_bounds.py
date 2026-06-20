"""Cardinality guard for the ADR-0190 expanded instruments.

Drives every new emitter into one in-memory meter and asserts that (1) every label *key*
on every data point is in ``ALLOWED_LABEL_KEYS`` (no identifier leaks), (2) every value of
the four new bounded labels is drawn from its declared enum/constant, and (3) the new metric
families render through the Prometheus text renderer.
"""

from __future__ import annotations

from typing import Any

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider

from kdive.domain.errors import ErrorCategory
from kdive.health.metrics_text import render_prometheus
from kdive.jobs.worker_telemetry import WorkerTelemetry
from kdive.observability.labels import ALLOWED_LABEL_KEYS
from kdive.reconciler.fleet import FleetSnapshot, FleetTelemetry
from kdive.reconciler.loop import ALL_REPAIR_KINDS
from kdive.reconciler.loop_telemetry import ReconcilerTelemetry
from kdive.services.allocation.admission.core import AdmissionOutcome
from kdive.services.allocation.admission.metrics import (
    AdmissionDecision,
    AdmissionMetrics,
    _AdmissionReason,
)
from tests.jobs.test_worker_telemetry import _job

from kdive.domain.capacity.state import (  # isort: skip
    AllocationState,
    DebugSessionState,
    JobState,
    RunState,
    SystemState,
)

# The declared bound for each new label key.
_STATE_VALUES = {
    s.value for enum in (AllocationState, SystemState, RunState, DebugSessionState) for s in enum
}
_BOUNDS: dict[str, set[str]] = {
    "repair_kind": set(ALL_REPAIR_KINDS),
    "state": _STATE_VALUES,
    "error_category": {c.value for c in ErrorCategory},
    "reason": {r.value for r in _AdmissionReason},
}
_OUTCOME_ADMISSION_VALUES = {d.value for d in AdmissionDecision}
_IDENTIFIER_KEYS = {"project", "principal", "object_id", "secret_ref", "tenant"}


def _emit_everything(reader: InMemoryMetricReader) -> None:
    meter = MeterProvider(metric_readers=[reader]).get_meter("test")
    tracer = TracerProvider().get_tracer("test")

    recon = ReconcilerTelemetry(tracer=tracer, meter=meter)
    recon.record_repairs({k: 1 for k in ALL_REPAIR_KINDS}, failures=["leaked_domains"])

    fleet = FleetTelemetry(meter=meter)
    fleet.refresh(
        FleetSnapshot(
            inventory={"allocations": {s.value: 1 for s in AllocationState}},
            capacity_used={"local-libvirt": 1},
            capacity_total={"local-libvirt": 2},
        )
    )

    admission = AdmissionMetrics(meter=meter)
    for category in (ErrorCategory.QUOTA_EXCEEDED, ErrorCategory.ALLOCATION_DENIED):
        admission.record_decision(
            AdmissionOutcome(granted=False, allocation=None, category=category)
        )
    admission.record_promotion(1.0)
    admission.record_queue_timeout(1)

    worker = WorkerTelemetry(tracer=tracer, meter=meter)
    worker.record_job_failure(
        _job(JobState.FAILED, ErrorCategory.BUILD_FAILURE), ErrorCategory.BUILD_FAILURE
    )


def _all_points(reader: InMemoryMetricReader) -> list[Any]:
    data = reader.get_metrics_data()
    assert data is not None
    points: list[Any] = []
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                points.extend(metric.data.data_points)
    return points


def test_every_label_key_is_allowlisted_and_no_identifier_leaks() -> None:
    reader = InMemoryMetricReader()
    _emit_everything(reader)
    for point in _all_points(reader):
        for key in point.attributes or {}:
            assert key in ALLOWED_LABEL_KEYS, f"unallowlisted label key {key!r}"
            assert key not in _IDENTIFIER_KEYS


def test_new_label_values_stay_within_their_bounded_enums() -> None:
    reader = InMemoryMetricReader()
    _emit_everything(reader)
    for point in _all_points(reader):
        attrs = point.attributes or {}
        for key, allowed in _BOUNDS.items():
            if key in attrs:
                assert attrs[key] in allowed, f"{key}={attrs[key]!r} escaped its bound"
        if "outcome" in attrs and attrs["outcome"] in _OUTCOME_ADMISSION_VALUES:
            assert attrs["outcome"] in _OUTCOME_ADMISSION_VALUES


def test_new_metric_families_render_to_prometheus_text() -> None:
    reader = InMemoryMetricReader()
    _emit_everything(reader)
    text = render_prometheus(reader.get_metrics_data())
    for family in (
        "kdive_reconciler_repairs",
        "kdive_allocations",
        "kdive_host_capacity_used",
        "kdive_allocation_admission",
        "kdive_errors",
    ):
        assert family in text, f"{family} did not render"
