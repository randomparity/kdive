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

That is **29 instruments** — the complete set the dashboard must cover.

The exact label sets are confirmed during implementation against the emitting
modules; the table above is the planning reference, not a frozen contract.

### Not metrics — explicitly excluded

These strings look like instrument names but are **not** OpenTelemetry instruments
and must never appear in a panel query or the coverage catalog:

- `kdive.config.core_settings`, `kdive.config.cli_settings`,
  `kdive.providers.local_libvirt.settings`,
  `kdive.providers.remote_libvirt.settings`,
  `kdive.providers.fault_inject.settings` — these are **Python module import
  paths** in `src/kdive/config/manifest.py` (`SETTING_MODULES`, ADR-0087). They are
  force-imported by `config/__init__.py` to aggregate each module's `SETTINGS`
  (`KDIVE_*` env-var registry); nothing feeds them to a meter, so they render no
  Prometheus series. There is no config/settings dashboard row.
- `kdive.mcp`, `kdive.worker`, `kdive.reconciler` — these are OTel **meter
  scope names** (`get_meter("kdive.worker")` etc. in `__main__.py`, `mcp/app.py`,
  `registrar.py`), not instruments; they have no series of their own.

## Dashboard layout — single dashboard, 9 collapsible rows

Exhaustive coverage of the **29 real instruments**: every emitted instrument gets
a panel. (There is no Info/settings row — the `kdive.config.*` /
`kdive.providers.*.settings` strings are config module paths, not metrics; see
"Not metrics — explicitly excluded" above.)

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
   to the base name); assert the set equals the instrument catalog. The catalog is
   enumerated by **static scan**, but precisely — a bare `"kdive\.…"` string match
   is wrong because it also captures meter *scope* names (`get_meter("kdive.mcp")`)
   and the `config/manifest.py` module paths, neither of which is an instrument.
   The guard therefore:
   - (a) scans an **explicit, named allowlist of telemetry source files** — exactly
     the modules in the catalog's Module column — and collects only the **first
     positional argument of `meter.create_counter/up_down_counter/histogram/`
     `observable_gauge(...)` calls** (an `ast-grep`/AST match on the call, not a
     regex over arbitrary strings). This structurally excludes scope names and
     config paths, which are never `create_*` arguments.
   - (b) expands the `reconciler/fleet.py:_INVENTORY` f-string (`f"kdive.{table}"`)
     from the hard-listed table set (`allocations`, `systems`, `runs`,
     `debug_sessions`) — the one instrument family whose name is not a literal.
   - (c) normalizes each OTel name to its rendered series with the **same rule as
     `metrics_text._sanitize`** (dots→`_`, **no `_total`**, no unit suffix).
   - (d) asserts a hard-coded exclusion set (`kdive.mcp`, `kdive.worker`,
     `kdive.reconciler` scope names; the five `SETTING_MODULES` paths) never leaks
     into the catalog, so a future maintainer who broadens the scan is caught.

   Anti-vacuity: the guard instantiates one real meter, renders via
   `render_prometheus`, and asserts (i) a concrete series is present under its true
   name and absent under the wrong one — `kdive_mcp_requests` present,
   `kdive_mcp_requests_total` absent — **and** (ii) the **count** of catalog base
   series equals 29 (25 literal `create_*` names + the 4 `_INVENTORY` gauges; note
   `kdive.errors` is emitted from two modules but is one series), so an over- or
   under-collection (e.g. a leaked scope name or a dropped instrument) fails
   immediately. Adding a real instrument later fails the set-equality assertion
   until the dashboard gets a panel.

A live smoke test (bring up the compose `obs` profile, import, eyeball) is
documented in the README as a manual step, not automated.

## Risks / open questions

- **Exact series names**: resolved, not open. The custom renderer
  (`metrics_text.py`) emits counters with **no `_total`** and no unit suffix; the
  naming contract above is authoritative and the coverage-guard's live-render
  assertion locks it against drift.
- **Catalog ≠ string-match**: two classes of `kdive.*` string (meter scope names,
  config `SETTING_MODULES` paths) are not instruments and are explicitly excluded
  from the catalog and every query; the coverage guard collects only `meter.create_*`
  arguments and a count assertion guards against re-contamination.
- **Empty panels without traffic**: on a freshly started stack some counters read
  zero; the README notes this and points at the compose `obs` profile plus a bit
  of exercised traffic for a populated view.
