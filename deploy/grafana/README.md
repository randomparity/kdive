# kdive — Grafana metrics dashboard

`kdive-overview.json` is a portable Grafana dashboard (Grafana 10+, schema v39) showing all 29
operational metrics kdive emits, grouped into nine collapsible subsystem rows.

## Import

1. In Grafana: **Dashboards → New → Import**, upload `kdive-overview.json`.
2. When prompted, pick the Prometheus datasource that scrapes your kdive deployment. The
   dashboard uses a `${datasource}` variable, so no UID editing is needed.

## What Prometheus must scrape

kdive runs three processes (server, worker, reconciler), each exposing its own
`/metrics` aux endpoint (ADR-0090 §5). Point Prometheus at **all three** — the reference
compose stack does this under the `obs` profile:

    docker compose --profile obs up -d prometheus

Metrics are served by a hand-rolled exposition renderer (`src/kdive/health/metrics_text.py`),
**not** the OpenTelemetry Prometheus exporter: counters have **no `_total` suffix** and there
are no unit suffixes. Off-the-shelf OTel dashboards will not match these series names.

## Empty panels

On a freshly started stack many counters read zero until traffic flows. Exercise a run
(allocate a system, start a build/debug session) to populate the request, admission, job,
and provider rows.

## Regenerating

The JSON is generated — do not hand-edit it. Edit `build_dashboard.py` and run:

    uv run python deploy/grafana/build_dashboard.py

A test (`tests/deploy/test_grafana_dashboard.py`) drift-guards the committed JSON against the
generator and asserts every emitted instrument has a panel.
