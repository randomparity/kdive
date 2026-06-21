"""Cardinality guard for the ADR-0190/0191 expanded instruments.

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

from kdive.domain.build_phase import BuildPhase
from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import ErrorCategory
from kdive.health.metrics_text import render_prometheus
from kdive.jobs.build_telemetry import BuildPhaseRecorder
from kdive.jobs.handlers.capture_telemetry import CaptureTelemetry
from kdive.jobs.provider_context import set_provider_kind
from kdive.jobs.worker_telemetry import WorkerTelemetry
from kdive.mcp.tools.debug.debug_session_telemetry import DebugSessionTelemetry
from kdive.observability.labels import ALLOWED_LABEL_KEYS
from kdive.reconciler.build_host_fleet import BuildHostSnapshot, BuildHostTelemetry
from kdive.reconciler.console_telemetry import ConsoleTelemetry
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
    # ADR-0191 §1: new bounded-enum labels from the second-cut instruments.
    "build_phase": {p.value for p in BuildPhase},
    "capture_method": {c.value for c in CaptureMethod},
    "transport": {"gdbstub", "drgn-live"},
}
_OUTCOME_ADMISSION_VALUES = {d.value for d in AdmissionDecision}
_IDENTIFIER_KEYS = {"project", "principal", "object_id", "secret_ref", "tenant"}

# Seeded build-host names used in _emit_everything — the bound for the build_host label.
# build_host is the documented deployment-bounded (non-enum) exception (ADR-0191 §1); it
# is not keyed to an enum in _BOUNDS, but its values are still asserted to be a subset of
# this declared operator set.
_SEEDED_BUILD_HOSTS = {"builder-01", "builder-02"}


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

    # F + I: provider-op RED (job_span with provider tag) + time-to-claim + retries.
    with worker.job_span("build") as job_handle:
        set_provider_kind("local-libvirt")
        job_handle.set_outcome("ok")
    worker.record_time_to_claim("build", 5.0)
    worker.record_job_retry("build")

    # G1: build sub-phase duration.
    build_recorder = BuildPhaseRecorder(meter=meter)
    with build_recorder.phase(BuildPhase.COMPILE, "local-libvirt"):
        pass

    # G2/G3: build-host lease/capacity/reachability gauges from a seeded snapshot.
    build_host_telem = BuildHostTelemetry(meter=meter)
    build_host_telem.refresh(
        BuildHostSnapshot(
            leases={h: i for i, h in enumerate(_SEEDED_BUILD_HOSTS)},
            capacity={h: 4 for h in _SEEDED_BUILD_HOSTS},
            reachable={h: 1.0 for h in _SEEDED_BUILD_HOSTS},
        )
    )

    # H1: vmcore capture duration + bytes.
    capture_telem = CaptureTelemetry(meter=meter)
    capture_telem.record(
        CaptureMethod.KDUMP.value,
        "remote-libvirt",
        "ok",
        seconds=30.0,
        size_bytes=500_000_000,
    )

    # H2: console bytes finalized.
    console_telem = ConsoleTelemetry(meter=meter)
    console_telem.record(135_664)

    # H3: debug-session duration (both regular close and reconciler reap).
    debug_telem = DebugSessionTelemetry(meter=meter)
    debug_telem.record("gdbstub", "ok", 120.0)
    debug_telem.record("drgn-live", "reaped", 300.0)


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
        # build_host is the deployment-bounded non-enum exception (ADR-0191 §1): assert its
        # values are drawn from the seeded operator set, not an enum.
        if "build_host" in attrs:
            assert attrs["build_host"] in _SEEDED_BUILD_HOSTS, (
                f"build_host={attrs['build_host']!r} not in the seeded host set"
            )


def _build_host_metrics(reader: InMemoryMetricReader) -> dict[str, Any]:
    by_name: dict[str, Any] = {}
    data = reader.get_metrics_data()
    assert data is not None
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name.startswith("kdive.build_host."):
                    by_name[metric.name] = metric
    return by_name


def test_build_host_gauges_emit_named_values_and_descriptions() -> None:
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader]).get_meter("test")
    telem = BuildHostTelemetry(meter=meter)
    telem.refresh(
        BuildHostSnapshot(
            leases={"builder-01": 3},
            capacity={"builder-01": 7},
            reachable={"builder-01": 1.0},
        )
    )
    metrics = _build_host_metrics(reader)
    assert set(metrics) == {
        "kdive.build_host.leases",
        "kdive.build_host.capacity",
        "kdive.build_host.reachable",
    }

    expected = {
        "kdive.build_host.leases": (3, "Active build-host lease count per host."),
        "kdive.build_host.capacity": (
            7,
            "Maximum concurrent build leases per host (max_concurrent).",
        ),
        "kdive.build_host.reachable": (
            1.0,
            "1.0 if the host is state=ready, 0.0 if state=unreachable.",
        ),
    }
    for name, (value, description) in expected.items():
        metric = metrics[name]
        assert metric.unit == "1"
        assert metric.description == description
        points = list(metric.data.data_points)
        assert len(points) == 1
        assert points[0].value == value
        assert dict(points[0].attributes) == {"build_host": "builder-01"}


def test_build_host_callbacks_yield_empty_before_first_refresh() -> None:
    meter = MeterProvider(metric_readers=[InMemoryMetricReader()]).get_meter("test")
    telem = BuildHostTelemetry(meter=meter)
    # The pre-first-pass empty snapshot makes every callback yield zero observations
    # rather than crashing on a missing snapshot.
    assert list(telem._leases_callback(None)) == []
    assert list(telem._capacity_callback(None)) == []
    assert list(telem._reachable_callback(None)) == []


def test_build_host_telemetry_refresh_replaces_snapshot() -> None:
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader]).get_meter("test")
    telem = BuildHostTelemetry(meter=meter)
    telem.refresh(BuildHostSnapshot(leases={"builder-02": 5}, capacity={}, reachable={}))
    leases = _build_host_metrics(reader)["kdive.build_host.leases"]
    points = list(leases.data.data_points)
    assert [(p.value, dict(p.attributes)) for p in points] == [(5, {"build_host": "builder-02"})]


def _fleet_metrics(reader: InMemoryMetricReader) -> dict[str, Any]:
    by_name: dict[str, Any] = {}
    data = reader.get_metrics_data()
    assert data is not None
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                by_name[metric.name] = metric
    return by_name


def _points_as_map(metric: Any, label_key: str) -> dict[str, Any]:
    return {dict(p.attributes)[label_key]: p.value for p in metric.data.data_points}


def test_fleet_inventory_gauge_emits_state_keyed_counts() -> None:
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader]).get_meter("test")
    telem = FleetTelemetry(meter=meter)
    telem.refresh(
        FleetSnapshot(
            inventory={
                "allocations": {
                    AllocationState.GRANTED.value: 2,
                    AllocationState.ACTIVE.value: 5,
                }
            },
            capacity_used={},
            capacity_total={},
        )
    )
    metric = _fleet_metrics(reader)["kdive.allocations"]
    assert metric.unit == "1"
    assert metric.description == "allocations grouped by lifecycle state (live count)."
    assert _points_as_map(metric, "state") == {
        AllocationState.GRANTED.value: 2,
        AllocationState.ACTIVE.value: 5,
    }


def test_fleet_capacity_gauges_keep_used_and_total_distinct() -> None:
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader]).get_meter("test")
    telem = FleetTelemetry(meter=meter)
    # Distinct used vs total values catch a used/total source swap.
    telem.refresh(
        FleetSnapshot(
            inventory={},
            capacity_used={"local-libvirt": 3},
            capacity_total={"local-libvirt": 9},
        )
    )
    metrics = _fleet_metrics(reader)
    used = metrics["kdive.host.capacity.used"]
    total = metrics["kdive.host.capacity.total"]
    assert used.unit == "1"
    assert total.unit == "1"
    assert used.description == "Host-cap slots occupied per provider."
    assert total.description == "Advertised host-cap slots per provider."
    assert _points_as_map(used, "provider") == {"local-libvirt": 3}
    assert _points_as_map(total, "provider") == {"local-libvirt": 9}


def test_fleet_callbacks_handle_missing_and_empty_snapshot() -> None:
    meter = MeterProvider(metric_readers=[InMemoryMetricReader()]).get_meter("test")
    telem = FleetTelemetry(meter=meter)
    # Pre-refresh empty snapshot: every callback yields zero observations, no crash.
    inv = telem._inventory_callback("allocations")
    used_cb = telem._capacity_callback(used=True)
    total_cb = telem._capacity_callback(used=False)
    assert list(inv(None)) == []
    assert list(used_cb(None)) == []
    assert list(total_cb(None)) == []
    # A snapshot missing the requested table yields no observations (not a crash).
    telem.refresh(FleetSnapshot(inventory={"runs": {}}, capacity_used={}, capacity_total={}))
    assert list(inv(None)) == []


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
        # ADR-0191 second-cut families:
        "kdive_provider_op_duration",
        "kdive_build_phase_duration",
        "kdive_build_host_leases",
        "kdive_build_host_reachable",
        "kdive_vmcore_capture_duration",
        "kdive_console_bytes",
        "kdive_debug_session_duration",
        "kdive_job_time_to_claim",
        "kdive_job_retries",
    ):
        assert family in text, f"{family} did not render"
