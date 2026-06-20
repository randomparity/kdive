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
  self-identifying by name, and the same instrument (`kdive_errors`) legitimately
  appears on more than one process (worker and reconciler), so over-filtering
  would hide data.

## Exposition naming conventions (the contract every query depends on)

kdive does **not** use the OpenTelemetry Prometheus exporter, and carries no
`opentelemetry-exporter-prometheus` / `prometheus_client` dependency. The
`/metrics` surface on every process is served by a hand-rolled renderer,
`src/kdive/health/metrics_text.py` (`render_prometheus`, wired in at
`src/kdive/health/aux_listener.py`). Its naming contract — read directly from the
renderer, not assumed from exporter defaults — is:

- **Name sanitization (`_sanitize`)**: every character that is not alphanumeric,
  `_`, or `:` becomes `_`. So dots become underscores:
  `kdive.reconcile.duration` → `kdive_reconcile_duration`.
- **Counters carry NO `_total` suffix.** `_render_sum` emits the bare sanitized
  name. `kdive.mcp.requests` → `kdive_mcp_requests` (not `..._total`). This is the
  single most important deviation from stock Prometheus conventions and the reason
  off-the-shelf OTel dashboard PromQL will not work here.
- **No unit suffix** (no `_seconds`, no `_bytes` appended by the renderer). A
  histogram declared with `unit="s"` still renders as `kdive_..._bucket`, etc.
- **Gauges** (observable lifecycle / capacity / queue-depth) keep the base name:
  `kdive.job.queue.depth` → `kdive_job_queue_depth`.
- **Histograms** split into `_bucket` (cumulative, with an `le="+Inf"` bucket),
  `_sum`, and `_count` series, each carrying an `le` label on the buckets.
  Confirmed by `tests/health/test_metrics_text.py` (which renders
  `kdive_request_duration_bucket` and, notably, `kdive_allocations` /
  `kdive_request_duration` with **no** `_total`).

A Prometheus scrape adds its own target labels (in the reference compose all three
processes share `job="kdive"`, distinguished by `instance`). Histogram bucket
boundaries come from the SDK's aggregation (instruments set
`explicit_bucket_boundaries_advisory`); `histogram_quantile` works regardless of
the exact boundaries, so the dashboard does not hardcode bucket values.

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

- Counter rate (**no `_total` suffix** — see naming contract above):
  `sum by (<dim>) (rate(<name>[$__rate_interval]))`, e.g.
  `sum by (outcome) (rate(kdive_mcp_requests[$__rate_interval]))`.
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
   `expr` strings (stripping the `_bucket`/`_sum`/`_count` histogram suffixes back
   to the base name); assert the set covers the full instrument catalog. The
   catalog is enumerated by **static scan**, not introspection — instruments are
   created inside methods via `meter.create_*("kdive…")` string literals, not as
   importable module constants, and the lifecycle gauges use an f-string
   (`f"kdive.{table}"` in `reconciler/fleet.py:_INVENTORY`). The guard therefore:
   (a) greps the telemetry modules for `"kdive\.[a-z0-9_.]+"` literals, (b) expands
   the `fleet.py:_INVENTORY` f-string names from the hard-listed table set
   (`allocations`, `systems`, `runs`, `debug_sessions`), and (c) normalizes each
   OTel name to its rendered series name using the **same rule as
   `metrics_text._sanitize`** (dots→`_`, **no `_total`**, no unit suffix).
   To keep the guard from going vacuous (green while the dashboard is broken on a
   live scrape), it also instantiates one real meter, renders via
   `render_prometheus`, and asserts a concrete series is present under its true
   name and absent under the wrong one — e.g. `kdive_mcp_requests` present,
   `kdive_mcp_requests_total` absent. Adding a new instrument later fails the
   coverage assertion until the dashboard gets a panel.

A live smoke test (bring up the compose `obs` profile, import, eyeball) is
documented in the README as a manual step, not automated.

## Risks / open questions

- **Exact series names**: resolved, not open. The custom renderer
  (`metrics_text.py`) emits counters with **no `_total`** and no unit suffix; the
  naming contract above is authoritative and the coverage-guard's live-render
  assertion locks it against drift.
- **Info-gauge label cardinality**: the settings gauges carry many labels; the
  Info row uses table panels rather than time series to keep them readable.
- **Empty panels without traffic**: on a freshly started stack some counters read
  zero; the README notes this and points at the compose `obs` profile plus a bit
  of exercised traffic for a populated view.
