"""Tests for the static metric-catalog enumerator backing the dashboard coverage guard."""

from __future__ import annotations

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from kdive.health.metrics_text import render_prometheus
from tests.deploy.grafana_catalog import catalog_series


def test_catalog_has_29_series() -> None:
    assert len(catalog_series()) == 29


def test_catalog_includes_known_instruments() -> None:
    series = catalog_series()
    for expected in (
        "kdive_mcp_requests",
        "kdive_allocation_admission",
        "kdive_reconciler_repairs",
        "kdive_job_queue_depth",
        "kdive_provider_op_duration",
        "kdive_console_bytes",
        "kdive_allocations",
        "kdive_debug_sessions",
        "kdive_host_capacity_total",
    ):
        assert expected in series


def test_catalog_excludes_scope_names_and_config_paths() -> None:
    series = catalog_series()
    for excluded in (
        "kdive_mcp",
        "kdive_worker",
        "kdive_reconciler",
        "kdive_config_core_settings",
        "kdive_providers_local_libvirt_settings",
    ):
        assert excluded not in series


def test_renderer_emits_counter_without_total_suffix() -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter("kdive.test")
    meter.create_counter("kdive.mcp.requests").add(1, {"tool": "runs.create"})
    body = render_prometheus(reader.get_metrics_data())
    assert "kdive_mcp_requests{" in body
    assert "kdive_mcp_requests_total" not in body
