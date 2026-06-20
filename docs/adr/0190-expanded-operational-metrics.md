# ADR 0190 — Expanded operational metrics (reconciler, lifecycle, admission, error taxonomy)

- **Status:** Accepted
- **Date:** 2026-06-19
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0090](0090-opentelemetry-adoption-service-health.md)
  (the per-process aux `/metrics` listener + §4 label allowlist this extends),
  [ADR-0189](0189-bundled-prometheus-metrics-collection.md) (the Prometheus that collects
  these series), [ADR-0019](0019-tool-response-envelope.md) (the error category enum group E counts).
- **Spec:** [`../design/expanded-operational-metrics.md`](../design/expanded-operational-metrics.md)
- **Related:** #601 (this), #600 (the collector), #561 (the pool work the admission metrics inform).

## Context

The aux `/metrics` endpoint (ADR-0090 §5) is now scraped by the opt-in bundled Prometheus
(ADR-0189, #600), but the emitted set is thin. Today's instruments:

| Process | Instrument | Type |
|---|---|---|
| worker (`kdive.worker`) | `kdive_job_duration` | histogram |
| worker | `kdive_job_queue_depth` | gauge |
| server (`kdive.mcp`) | `kdive_mcp_requests` / `kdive_mcp_request_errors` / `kdive_mcp_request_duration` | counter / counter / histogram |
| reconciler (`kdive.reconciler`) | `kdive_reconcile_duration` / `kdive_reconcile_lag` | histogram / histogram |

Gaps (#601): the reconciler emits pass duration/lag but **nothing about the repairs it
performs**; there is no fleet inventory ("what exists right now"); admission decisions are
invisible (the exact signal #561's pool work wants); and failures are not broken down by
the ADR-0019 error taxonomy.

The governing constraint is the ADR-0090 §4 label allowlist
(`observability/labels.py` `ALLOWED_LABEL_KEYS`): a metric/span label may only carry a
reviewed, low-cardinality key. High-cardinality identity (`project`, `principal`,
`object_id`, `secret_ref`) is excluded — per ADR-0089 it travels on the access-controlled
log path, never as a metric label. Two allowlisted keys are reserved-but-unused:
`provider` and `transition_kind`.

This ADR covers the issue's "first cut" — groups A (reconciler repairs), B (lifecycle
inventory), D (admission/capacity), and E (error taxonomy). Provider-op RED (F), build
sub-phase timings (G), capture/debug counters (H), and extended job/queue health (I) are
deferred to a follow-up once these labels and dashboards settle.

## Decision

### 1. New labels (allowlist additions)

Add four low-cardinality keys to `ALLOWED_LABEL_KEYS`, each bounded by an enum:

| Key | Bounded by | Cardinality |
|---|---|---|
| `repair_kind` | the static reconciler repair plan (`ALL_REPAIR_KINDS`) | ~21 |
| `state` | the per-object state enums (`AllocationState`/`SystemState`/`RunState`/`DebugSessionState`) | ≤7 per object |
| `error_category` | `ErrorCategory` (ADR-0019) | 22 |
| `reason` | `_AdmissionReason` | 8 |

`outcome` (already allowlisted) gains the admission values `{granted, rejected, queued}`
alongside the existing `{ok, error}`. A cardinality-guard test asserts every emitted value
of each label is drawn from its declared bounded set — the test is the enforcement, in
addition to `filter_label_keys` dropping any non-allowlisted *key*.

### 2. A — reconciler repairs (`kdive.reconciler`)

`kdive_reconciler_repairs_total` counter, label `repair_kind`. `reconcile_once` already
returns per-kind counts keyed by the exact `_RepairSpec.name` strings; `ReconcilerTelemetry`
gains `record_repairs(counts, failures)` called once per pass, adding each kind's count
(including 0, so the series exists from the start). A repair that raised this pass
(`failures`) additionally increments `kdive_errors_total` under
`error_category=infrastructure_failure` (group E), so a wedged sweep is visible even though
its repair count is 0.

`repair_kind` values are the real spec names (e.g. `orphaned_systems`,
`reaped_build_vms`, `promoted_allocations`), declared as `ALL_REPAIR_KINDS`. A test asserts
`_repair_plan` with every optional port enabled produces exactly that set, so the bound and
the plan never drift.

### 3. B — lifecycle inventory (`kdive.reconciler`)

Observable gauges `kdive_allocations` / `kdive_systems` / `kdive_runs` /
`kdive_debug_sessions`, each labeled `state`, reporting the live count of that object in
that state. The reconciler refreshes a `FleetSnapshot` (count-by-state per object, plus
group-D host capacity) once per pass and the sync gauge callbacks emit from that cache —
mirroring the worker's queue-depth caching (`observe_queue_depth` → `_last_depth`), because
an OTel observable-gauge callback is synchronous and cannot `await` the async pool. Refresh
cadence is the reconcile interval; a stale snapshot (reconciler down) yields absent series,
which the reconciler's own liveness already surfaces.

The FIFO **pending** depth is `kdive_allocations{state="requested"}`; no separate
`kdive_allocation_pending` gauge is added (it would duplicate this series).

### 4. D — admission & capacity

`kdive_allocation_admission_total` counter, labels `outcome` ∈ {granted, rejected, queued}
and `reason` ∈ `_AdmissionReason` {none, quota, budget, capacity, affinity, pcie,
configuration, queue_timeout, unknown}. An `AdmissionMetrics` emitter classifies an `AdmissionOutcome`
into the `(outcome, reason)` pair via a pure mapping that keys on the **full outcome shape**,
not the category alone, because both the success flag and the categories are overloaded:

- an enqueued request is a *success* outcome (`granted=True`) carrying a `REQUESTED`
  allocation (`_enqueue`), so `queued` vs `granted` is read from `allocation.state`, never
  from `granted` alone;
- input-validation and PCIe-grammar denials both raise `CONFIGURATION_ERROR` with no
  distinguishing reason, so they fold into one `configuration` reason — never `pcie`;
- the PCIe-busy denial is the only `ALLOCATION_DENIED` that sets no `reason` string
  (`reason=None`), so `pcie` is matched by elimination after the three reason-bearing
  `ALLOCATION_DENIED` shapes (budget/affinity/capacity);
- an unmatched `(category, reason)` maps to `(rejected, unknown)` — a distinct sentinel,
  never sharing `none` with a successful outcome — with a `warning` log so a new denial shape
  surfaces rather than silently mislabeling.

It is recorded at the synchronous `admit()` boundary in the allocations tool handler
(`kdive.mcp`) and at the promotion / queue-timeout sweeps in the reconciler
(`kdive.reconciler`).

`kdive_allocation_wait_seconds` histogram (request→grant latency) is recorded at promotion
only (`now - allocation.created_at`); a synchronous grant waits ~0 and is not recorded.
`promote_pending` / `reap_queue_timeouts` take an optional `AdmissionMetrics` (default the
no-op) and record inside the per-candidate loop where the `Allocation` row (with
`created_at`) is in hand; the int count return is unchanged.

`kdive_host_capacity_used` / `kdive_host_capacity_total` gauges, labeled `provider`
(reserved key, now used), come from the same per-pass `FleetSnapshot`: used = occupying
allocations (`GRANTED`/`ACTIVE`/`RELEASING`) per provider; total = sum of advertised
`concurrent_allocation_cap` per provider.

### 5. E — error taxonomy

`kdive_errors_total` counts categorized failures **at their origin**, so one root cause is
counted once, not once per poll. A worker job failure is echoed back through
`ToolResponse.from_job` on every `jobs.get`/`jobs.list` poll; incrementing a server-side
`kdive_errors` on each echo would inflate the count by the poll rate. The split:

- **server** — no new server counter. The existing `kdive_mcp_request_errors` counter (a
  per-call RED rate, already incremented once per failed tool call) gains an `error_category`
  label, giving the request surface a by-category breakdown with no new double-counting.
- **worker** — `kdive_errors_total{error_category}` increments once per job→`FAILED`
  transition at the shared `queue.fail` seam (the worker fails a job at more than one site —
  pre-dispatch and handler-exception — so the counter hooks the common transition, not one
  branch).
- **reconciler** — `kdive_errors_total` increments under `infrastructure_failure` per repair
  named in a pass's `failures` (§2).

`kdive_errors_total` is thus the backend-origin failure counter (worker + reconciler) with no
poll inflation; the request-surface error rate by category lives on
`kdive_mcp_request_errors{tool,error_category}`. `error_category` values are bounded by
`ErrorCategory`.

### 6. No schema / migration change

Every instrument reads existing rows or in-process state; nothing is persisted. Metrics
emit on the existing aux `/metrics` per process and are aggregated across processes by the
ADR-0189 Prometheus.

## Consequences

- The reconciler gains DB read load of one count-by-state + capacity aggregate per pass
  (a handful of grouped `COUNT(*)` queries), independent of scrape rate. Acceptable: the
  reconciler already opens a connection per repair.
- New series are bounded: `repairs_total` ~21, inventory gauges ≤7×4, admission ≤3×7,
  `errors_total` ≤22, capacity 2×|providers|. No per-object or per-tenant label.
- Dashboards/alerts can break failures down by category, watch queue wait latency for the
  pool work (#561), and show an instant fleet inventory.
- The cardinality-guard test fails loudly if a new label key escapes the allowlist or a
  label value escapes its bounded enum.

## Considered & rejected

- **The issue's 8-value `repair_kind` enum** (orphaned_system_teardown, dead_lease_reclaim,
  …) — a semantic grouping that requires a hand-maintained spec-name→group mapping that
  drifts from the real repair plan. The actual `_RepairSpec.name` set is already bounded and
  is the natural instrumentation key.
- **Reading the DB on every scrape for the inventory gauges** — an OTel observable callback
  is synchronous and cannot await the async pool, and it would couple metric cost to scrape
  rate. Caching a per-pass snapshot solves both.
- **A separate `kdive_allocation_pending` gauge** — duplicates
  `kdive_allocations{state="requested"}`.
- **Hosting the inventory gauges on the server** — adds DB load to the latency-sensitive
  request process; the reconciler already runs a periodic loop with a pool.
- **A new dedicated metrics/exporter process** — each process already exposes its own aux
  `/metrics`; Prometheus aggregates.
- **Restricting admission `outcome` to {granted, rejected}** — a queueable denial that
  enqueues is neither; `queued` is the honest third value.

## Related

- ADR-0090 (§4 allowlist, §5 aux listener), ADR-0089 (identity on the log path),
  ADR-0019 (error taxonomy), ADR-0189/#600 (the Prometheus that collects these), #561 (the
  pool work the admission metrics inform).
