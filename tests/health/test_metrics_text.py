"""Prometheus text rendering tests for health metrics."""

from __future__ import annotations

from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    Histogram,
    HistogramDataPoint,
    Metric,
    MetricsData,
    ResourceMetrics,
    ScopeMetrics,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.util.instrumentation import InstrumentationScope

from kdive.health.metrics_text import render_prometheus


def test_histogram_renders_prometheus_bucket_sum_and_count_lines() -> None:
    metrics = MetricsData(
        resource_metrics=[
            ResourceMetrics(
                resource=Resource.create({}),
                scope_metrics=[
                    ScopeMetrics(
                        scope=InstrumentationScope("test"),
                        metrics=[
                            Metric(
                                name="kdive.request-duration",
                                description="Request duration",
                                unit="s",
                                data=Histogram(
                                    data_points=[
                                        HistogramDataPoint(
                                            attributes={
                                                "z_label": 'needs"escape',
                                                "a_label": "line\nbreak",
                                            },
                                            start_time_unix_nano=1,
                                            time_unix_nano=2,
                                            count=6,
                                            sum=1.75,
                                            bucket_counts=[1, 2, 3],
                                            explicit_bounds=[0.1, 1.0],
                                            min=0.05,
                                            max=1.4,
                                        )
                                    ],
                                    aggregation_temporality=AggregationTemporality.CUMULATIVE,
                                ),
                            )
                        ],
                        schema_url="",
                    )
                ],
                schema_url="",
            )
        ]
    )

    body = render_prometheus(metrics)

    assert "# TYPE kdive_request_duration histogram\n" in body
    assert (
        'kdive_request_duration_bucket{a_label="line\\nbreak",le="0.1",'
        'z_label="needs\\"escape"} 1\n'
    ) in body
    assert (
        'kdive_request_duration_bucket{a_label="line\\nbreak",le="1.0",'
        'z_label="needs\\"escape"} 3\n'
    ) in body
    assert (
        'kdive_request_duration_bucket{a_label="line\\nbreak",le="+Inf",'
        'z_label="needs\\"escape"} 6\n'
    ) in body
    assert (
        'kdive_request_duration_sum{a_label="line\\nbreak",z_label="needs\\"escape"} 1.75\n'
    ) in body
    assert (
        'kdive_request_duration_count{a_label="line\\nbreak",z_label="needs\\"escape"} 6\n'
    ) in body


def test_gauge_renders_a_sample_per_data_point() -> None:
    from opentelemetry.sdk.metrics.export import Gauge, NumberDataPoint

    metrics = MetricsData(
        resource_metrics=[
            ResourceMetrics(
                resource=Resource.create({}),
                scope_metrics=[
                    ScopeMetrics(
                        scope=InstrumentationScope("test"),
                        metrics=[
                            Metric(
                                name="kdive.allocations",
                                description="Allocations by state",
                                unit="1",
                                data=Gauge(
                                    data_points=[
                                        NumberDataPoint(
                                            attributes={"state": "granted"},
                                            start_time_unix_nano=1,
                                            time_unix_nano=2,
                                            value=3,
                                        )
                                    ]
                                ),
                            )
                        ],
                        schema_url="",
                    )
                ],
                schema_url="",
            )
        ]
    )
    body = render_prometheus(metrics)
    assert "# TYPE kdive_allocations gauge\n" in body
    assert 'kdive_allocations{state="granted"} 3\n' in body


def _wrap(*metrics: Metric) -> MetricsData:
    return MetricsData(
        resource_metrics=[
            ResourceMetrics(
                resource=Resource.create({}),
                scope_metrics=[
                    ScopeMetrics(
                        scope=InstrumentationScope("test"),
                        metrics=list(metrics),
                        schema_url="",
                    )
                ],
                schema_url="",
            )
        ]
    )


def _sum_metric(name: str, description: str, *, value: int = 1) -> Metric:
    from opentelemetry.sdk.metrics.export import NumberDataPoint, Sum

    return Metric(
        name=name,
        description=description,
        unit="1",
        data=Sum(
            data_points=[
                NumberDataPoint(
                    attributes={},
                    start_time_unix_nano=1,
                    time_unix_nano=2,
                    value=value,
                )
            ],
            aggregation_temporality=AggregationTemporality.CUMULATIVE,
            is_monotonic=True,
        ),
    )


def test_render_joins_lines_with_newline_only() -> None:
    # The body is each line plus a single trailing newline, joined with nothing else;
    # a counter with a description renders exactly HELP, TYPE, sample, each on its own line.
    body = render_prometheus(_wrap(_sum_metric("kdive.requests", "Total requests", value=5)))
    assert body == (
        "# HELP kdive_requests Total requests\n# TYPE kdive_requests counter\nkdive_requests 5\n"
    )


def test_repeated_metric_name_emits_help_and_type_once() -> None:
    # _emit_help dedupes on the metric name, so two metrics sharing a name produce only
    # one HELP and one TYPE block (the second metric still emits its sample).
    body = render_prometheus(
        _wrap(
            _sum_metric("kdive.dupe", "First", value=1),
            _sum_metric("kdive.dupe", "Second", value=2),
        )
    )
    assert body.count("# TYPE kdive_dupe counter\n") == 1
    assert body.count("# HELP kdive_dupe First\n") == 1
    assert "# HELP kdive_dupe Second\n" not in body
    assert "kdive_dupe 1\n" in body
    assert "kdive_dupe 2\n" in body


def test_help_line_carries_the_metric_description() -> None:
    # The HELP line text is the metric description, not a placeholder.
    body = render_prometheus(_wrap(_sum_metric("kdive.described", "A clear description")))
    assert "# HELP kdive_described A clear description\n" in body
    assert "None" not in body


def test_escape_doubles_backslashes_in_label_values() -> None:
    from opentelemetry.sdk.metrics.export import NumberDataPoint, Sum

    metric = Metric(
        name="kdive.paths",
        description="Paths",
        unit="1",
        data=Sum(
            data_points=[
                NumberDataPoint(
                    attributes={"path": "a\\b"},
                    start_time_unix_nano=1,
                    time_unix_nano=2,
                    value=1,
                )
            ],
            aggregation_temporality=AggregationTemporality.CUMULATIVE,
            is_monotonic=True,
        ),
    )
    body = render_prometheus(_wrap(metric))
    # A backslash in a label value is doubled (Prometheus escaping), exactly once.
    assert 'kdive_paths{path="a\\\\b"} 1\n' in body
