# kdive — Grafana metrics dashboard

`kdive-overview.json` is the generated operational dashboard. It groups emitted metrics by
subsystem and uses a selectable Prometheus datasource. The generator and its coverage test
own the panel and instrument inventory; this page owns import and scrape setup.

## Import

1. In Grafana: **Dashboards → New → Import**, upload `kdive-overview.json`.
2. When prompted, pick the Prometheus datasource that scrapes your kdive deployment. The
   dashboard uses a `${datasource}` variable, so no UID editing is needed.

## What Prometheus must scrape

The portable core has server, worker, and reconciler processes, each with its own `/metrics`
aux endpoint. Kubernetes also runs lifecycle-witness. Scrape each deployed role; the
reference Compose stack configures its core processes under the `obs` profile:

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
