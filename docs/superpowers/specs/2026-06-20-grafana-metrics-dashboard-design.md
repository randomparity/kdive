# kdive Metrics Showcase — Grafana Dashboard Design

**Date:** 2026-06-20
**Status:** Approved (brainstorming)
**Scope:** A single, portable Grafana dashboard that visualizes the full set of
operational metrics kdive emits today (ADR-0090/0189/0190/0191). No new metrics,
no alerting, no deploy-stack wiring.

## Goal

Give an operator or evaluator one importable dashboard that showcases every
operational metric kdive exposes, grouped by subsystem, so the telemetry surface
is legible at a glance and demoable end to end.

## Non-goals

- No new instruments — strictly visualizes what is emitted today.
- No alerting rules, recording rules, or SLO definitions.
- No wiring into the reference compose stack or the Helm chart. The artifact is a
  portable dashboard JSON plus a README; it imports into any Grafana pointed at a
  Prometheus that scrapes a kdive deployment.

## Artifacts

| Path | Purpose |
|------|---------|
| `deploy/grafana/kdive-overview.json` | The dashboard model (Grafana 10+, schema v39). |
| `deploy/grafana/README.md` | Import instructions, datasource expectations, scrape note. |
| `tests/deploy/test_grafana_dashboard.py` | JSON validity + coverage-guard against the live metric catalog. |

## Portability mechanics

- **`$datasource`** template variable (type `datasource`, query `prometheus`).
  Every panel targets `${datasource}`, so import works on any Grafana without
  editing datasource UIDs.
- Rate windows use **`$__rate_interval`** so they auto-scale with the zoom level.
- No hardcoded `job=`/`instance=` selectors in queries. kdive metrics are
  self-identifying by name, and the same instrument (`kdive_errors_total`)
  legitimately appears on more than one process (worker and reconciler), so
  over-filtering would hide data.

## Exporter naming conventions (the contract every query depends on)

The OpenTelemetry Prometheus exporter transforms the OTel dotted instrument names
into Prometheus series:

- Dots become underscores: `kdive.reconcile.duration` → `kdive_reconcile_duration`.
- Monotonic counters gain a `_total` suffix: `kdive.mcp.requests` →
  `kdive_mcp_requests_total`.
- Histograms split into `_bucket` / `_sum` / `_count` series (confirmed against
  `tests/health/test_metrics_text.py`). No unit suffix (`_seconds`) is appended
  in this codebase's exporter configuration.
- Observable gauges keep the base name: `kdive.job.queue.depth` →
  `kdive_job_queue_depth`.

The exact `_total` suffixes and the absence of unit suffixes are **verified during
implementation** by rendering the real exposition (instantiating the meters and
reading the Prometheus output, or scraping a live `/metrics`). The coverage-guard
test (below) pins the names so they cannot silently drift.

## Metric catalog (source of truth)

Instruments emitted today, by defining module:

| Instrument (OTel name) | Type | Key labels | Module |
|------------------------|------|------------|--------|
| `kdive.mcp.requests` | counter | `tool`, `outcome` | `mcp/middleware/telemetry.py` |
| `kdive.mcp.request.errors` | counter | `tool` | `mcp/middleware/telemetry.py` |
| `kdive.mcp.request.duration` | histogram | `tool` | `mcp/middleware/telemetry.py` |
| `kdive.debug.session.duration` | histogram | — | `mcp/tools/debug/debug_session_telemetry.py` |
| `kdive.allocation.admission` | counter | `outcome`, `reason` | `services/allocation/admission/metrics.py` |
| `kdive.allocation.wait` | histogram | — | `services/allocation/admission/metrics.py` |
| `kdive.allocations` | gauge | `state` | `reconciler/fleet.py` |
| `kdive.systems` | gauge | `state` | `reconciler/fleet.py` |
| `kdive.runs` | gauge | `state` | `reconciler/fleet.py` |
| `kdive.debug_sessions` | gauge | `state` | `reconciler/fleet.py` |
| `kdive.host.capacity.used` | gauge | `provider` | `reconciler/fleet.py` |
| `kdive.host.capacity.total` | gauge | `provider` | `reconciler/fleet.py` |
| `kdive.reconcile.duration` | histogram | — | `reconciler/loop_telemetry.py` |
| `kdive.reconcile.lag` | histogram | — | `reconciler/loop_telemetry.py` |
| `kdive.reconciler.repairs` | counter | `repair_kind`, `outcome` | `reconciler/loop_telemetry.py` |
| `kdive.errors` | counter | `error_category` | `reconciler/loop_telemetry.py`, `jobs/worker_telemetry.py` |
| `kdive.job.duration` | histogram | `job_kind`, `outcome` | `jobs/worker_telemetry.py` |
| `kdive.job.queue.depth` | gauge | — | `jobs/worker_telemetry.py` |
| `kdive.job.retries` | counter | `job_kind` | `jobs/worker_telemetry.py` |
| `kdive.job.time_to_claim` | histogram | `job_kind` | `jobs/worker_telemetry.py` |
| `kdive.provider.op.duration` | histogram | `provider`, `job_kind` | `jobs/worker_telemetry.py` |
| `kdive.provider.op.errors` | counter | `provider`, `job_kind` | `jobs/worker_telemetry.py` |
| `kdive.build.phase.duration` | histogram | `build_phase`, `provider`, `outcome` | `jobs/build_telemetry.py` |
| `kdive.build_host.capacity` | gauge | `build_host` | `reconciler/build_host_fleet.py` |
| `kdive.build_host.leases` | gauge | `build_host` | `reconciler/build_host_fleet.py` |
| `kdive.build_host.reachable` | gauge | `build_host` | `reconciler/build_host_fleet.py` |
| `kdive.vmcore.capture.duration` | histogram | — | `jobs/handlers/capture_telemetry.py` |
| `kdive.vmcore.capture.bytes` | histogram | — | `jobs/handlers/capture_telemetry.py` |
| `kdive.console.bytes` | counter | — | `reconciler/console_telemetry.py` |
| `kdive.config.core_settings` | info gauge | (settings labels) | config telemetry |
| `kdive.config.cli_settings` | info gauge | (settings labels) | config telemetry |
| `kdive.providers.local_libvirt.settings` | info gauge | (settings labels) | provider telemetry |
| `kdive.providers.remote_libvirt.settings` | info gauge | (settings labels) | provider telemetry |
| `kdive.providers.fault_inject.settings` | info gauge | (settings labels) | provider telemetry |

The exact label sets are confirmed during implementation against the emitting
modules; the table above is the planning reference, not a frozen contract.

## Dashboard layout — single dashboard, 10 collapsible rows

Exhaustive coverage: every operational instrument gets a real panel; the static
info/settings gauges are collected into one collapsed Info row so they do not
dilute the storytelling.

1. **MCP request plane** — request rate by `tool`; error rate; request-duration
   p50/p95/p99 via `histogram_quantile` over `_bucket`; debug-session duration.
2. **Allocation / admission** — stacked admission decisions by `outcome`;
   rejection breakdown by `reason` (headline panel); queue-wait p95.
3. **Lifecycle inventory** — `kdive_allocations` / `kdive_systems` /
   `kdive_runs` / `kdive_debug_sessions` stacked by `state`.
4. **Capacity / saturation** — `kdive_host_capacity_used ÷ kdive_host_capacity_total`
   per `provider` as a bar gauge.
5. **Reconciler loop** — reconcile p95; reconcile lag p95; repair rate by
   `repair_kind`; error taxonomy by `error_category`.
6. **Jobs / workers** — job duration p95 by `job_kind`; queue depth; retry rate;
   time-to-claim p95.
7. **Build plane** — build-phase duration by `build_phase`; build-host fleet
   table (`capacity` / `leases` / `reachable` by `build_host`).
8. **Provider operations** — provider-op RED: duration p95 and error rate by
   `provider` / `job_kind`.
9. **Capture** — vmcore capture duration; vmcore capture-size distribution;
   console-bytes rate.
10. **ℹ️ Info (collapsed)** — config/provider settings gauges as label tables.

## PromQL patterns

- Counter rate: `sum by (<dim>) (rate(<name>_total[$__rate_interval]))`
- Histogram quantile: `histogram_quantile(0.95, sum by (le, <dim>) (rate(<name>_bucket[$__rate_interval])))`
- Gauge breakdown: `sum by (state) (<name>)`
- Saturation: `sum by (provider) (kdive_host_capacity_used) / sum by (provider) (kdive_host_capacity_total)`

## Validation strategy

`tests/deploy/test_grafana_dashboard.py`:

1. **JSON validity** — the file parses and has the expected top-level dashboard
   keys (`panels`, `templating`, `schemaVersion`, `title`).
2. **Datasource portability** — every panel/target references `${datasource}`;
   no hardcoded datasource UID leaks in.
3. **Coverage guard** — extract every `kdive_*` base series referenced in panel
   `expr` strings; assert the set covers the full instrument catalog enumerated
   from the meter modules (same drift-guard pattern the repo already uses for
   generated docs and tool schemas). Adding a new instrument later fails this
   test until the dashboard gets a panel.

A live smoke test (bring up the compose `obs` profile, import, eyeball) is
documented in the README as a manual step, not automated.

## Risks / open questions

- **Exact series names**: the `_total` suffix and unit-suffix behavior are
  exporter-dependent; pinned by rendering the real exposition during
  implementation and locking via the coverage-guard test.
- **Info-gauge label cardinality**: the settings gauges carry many labels; the
  Info row uses table panels rather than time series to keep them readable.
- **Empty panels without traffic**: on a freshly started stack some counters read
  zero; the README notes this and points at the compose `obs` profile plus a bit
  of exercised traffic for a populated view.
