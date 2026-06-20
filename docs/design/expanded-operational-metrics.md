# Spec: Expanded operational metrics (#601, first cut)

Status: accepted · ADR: [0190](../adr/0190-expanded-operational-metrics.md) · Issue: #601

## Goal

Emit the high-value operational metrics the bundled Prometheus (ADR-0189, #600) can now
collect, within the ADR-0090 §4 label allowlist. First cut = groups A (reconciler repairs),
B (lifecycle inventory), D (admission/capacity), E (error taxonomy). F/G/H/I deferred to a
follow-up issue.

## Non-goals

- No new persisted state, schema, or migration — every instrument reads existing rows or
  in-process values.
- No per-object / per-tenant label. Identifiers stay on the log path (ADR-0089).
- No change to the existing instruments or to the aux `/metrics` transport (ADR-0090 §5).
- Provider-op RED (F), build sub-phase timings (G), capture/debug (H), extended job/queue
  health (I).

## Instruments

All instrument names use the existing dotted convention (`kdive.<area>.<name>`), which the
Prometheus renderer (`health/metrics_text.py`) maps to `kdive_<area>_<name>`.

| # | Instrument | Type | Process(es) | Labels |
|---|---|---|---|---|
| A | `kdive.reconciler.repairs` | counter | reconciler | `repair_kind` |
| B | `kdive.allocations` / `kdive.systems` / `kdive.runs` / `kdive.debug_sessions` | observable gauge | reconciler | `state` |
| D | `kdive.allocation.admission` | counter | server, reconciler | `outcome`, `reason` |
| D | `kdive.allocation.wait` (s) | histogram | reconciler | — |
| D | `kdive.host.capacity.used` / `kdive.host.capacity.total` | observable gauge | reconciler | `provider` |
| E | `kdive.errors` | counter | worker, reconciler | `error_category` |
| E | `kdive.mcp.request.errors` (extend existing) | counter | server | `tool`, `outcome`, `error_category` |

## Labels (allowlist additions)

`ALLOWED_LABEL_KEYS` gains `repair_kind`, `state`, `error_category`, `reason`. Bounded by:

- `repair_kind` → `ALL_REPAIR_KINDS` (the full set of `_RepairSpec.name`, ~21).
- `state` → `AllocationState` ∪ `SystemState` ∪ `RunState` ∪ `DebugSessionState`.
- `error_category` → `ErrorCategory` (22).
- `reason` → `_AdmissionReason` = {none, quota, budget, capacity, affinity, pcie,
  configuration, queue_timeout, unknown} (9).
- `outcome` (already allowlisted) gains values {granted, rejected, queued}.

## Design details

### A — reconciler repairs

- `ReconcilerTelemetry` gains a `kdive.reconciler.repairs` counter and
  `record_repairs(counts: Mapping[str, int], failures: Iterable[str])`.
- `Reconciler._pass_loop` calls `record_repairs(report-counts, report.failures)` after each
  `run_once()`. The counts dict is keyed by `_RepairSpec.name`. Each kind's count is added
  every pass (including 0, so the series is present from start).
- For each name in `failures`, also increment `kdive.errors` under
  `error_category=infrastructure_failure` (E), so a wedged sweep is visible.
- `ALL_REPAIR_KINDS` is a module constant. A test builds `_repair_plan` with every optional
  port supplied and asserts the produced names == `ALL_REPAIR_KINDS`, pinning the bound to
  the plan.

### B + D-gauges — fleet snapshot

- New `reconciler/fleet.py`: a `FleetSnapshot` dataclass and an async
  `read_fleet_snapshot(conn) -> FleetSnapshot` that runs grouped `COUNT(*)` queries:
  - count-by-state for `allocations`, `systems`, `runs`, `debug_sessions`. The snapshot
    **seeds every state of each object's enum to 0** before applying the grouped counts, so a
    state that drops to zero still emits a `0` series rather than vanishing (a grouped
    `COUNT(*)` only returns states that currently have rows).
  - host capacity: occupying-allocation count per provider, and advertised
    `concurrent_allocation_cap` sum per provider (joined via the resource catalog).
- `FleetTelemetry` (new, in `loop_telemetry.py` or a sibling) holds the cached snapshot and
  registers the observable gauges whose sync callbacks read the cache. `refresh(snapshot)`
  swaps the cache; `disabled()` is the no-op.
- `Reconciler._pass_loop` reads a snapshot once per pass (its own pooled connection) and
  calls `FleetTelemetry.refresh`. A snapshot read failure is logged and leaves the previous
  cache (best-effort, like a repair).
- Gauge callbacks emit one `Observation(count, {"state": s})` per (object, state) and
  `Observation(n, {"provider": p})` for capacity used/total.

### D — admission counter + wait histogram

- New `services/allocation/admission/metrics.py`: `_AdmissionReason` enum, a pure
  `classify(outcome: AdmissionOutcome) -> tuple[str, str]` returning `(outcome, reason)`,
  and an `AdmissionMetrics` emitter (`record_decision(outcome)`, `record_wait(seconds)`,
  `disabled()`).
- `_AdmissionReason` = {none, quota, budget, capacity, affinity, pcie, configuration,
  queue_timeout, unknown} (9). `outcome` ∈ {granted, rejected, queued}.
- `classify` keys on the **full outcome shape**, not the category alone, because the success
  flag and the error categories are both overloaded:
  - **`granted=True` and `allocation.state == GRANTED`** → (`granted`, `none`).
  - **`granted=True` and `allocation.state == REQUESTED`** → (`queued`, `none`). A queueable
    denial that `on_capacity=queue` enqueues returns a *success* outcome carrying a
    `REQUESTED` allocation (`_enqueue` in `admission/core.py`), so `granted` alone cannot
    distinguish a real grant from an enqueue — the allocation's state is the discriminator.
  - `granted=False`, `QUOTA_EXCEEDED` → (`rejected`, `quota`) — covers both the grant-quota
    and the pending-cap denial (both raise `QUOTA_EXCEEDED`).
  - `granted=False`, `ALLOCATION_DENIED` + `reason == budget_exceeded` → (`rejected`,
    `budget`).
  - `granted=False`, `ALLOCATION_DENIED` + `reason == affinity_denied` → (`rejected`,
    `affinity`).
  - `granted=False`, `ALLOCATION_DENIED` + `reason == at_capacity` → (`rejected`,
    `capacity`).
  - `granted=False`, `ALLOCATION_DENIED` + `reason is None` → (`rejected`, `pcie`). The
    PCIe-busy denial (`_resolve_pcie_claim`, `MatchOutcome.CAPACITY`) is the **only**
    `ALLOCATION_DENIED` that sets **no `reason` string** (`reason=None`, queueable), so it is
    identified by elimination — there is no `pcie` reason literal. The three reason-bearing
    `ALLOCATION_DENIED` shapes (`budget_exceeded`/`affinity_denied`/`at_capacity`) are matched
    above, so a `None` reason here is unambiguously PCIe-busy.
  - `granted=False`, `CONFIGURATION_ERROR` → (`rejected`, `configuration`). Both the
    input-validation denial (`price_window_and_estimate`: bad window/size/over-caps) and the
    PCIe-grammar denial (`MatchOutcome.CONFIG`) raise `CONFIGURATION_ERROR` with no
    distinguishing reason, so they fold into one operator-fixable `configuration` reason —
    they are **not** labeled `pcie`.
  - queue-timeout reaper → (`rejected`, `queue_timeout`).
  - Any unmatched `(category, reason)` → (`rejected`, `unknown`) with a one-line `warning`
    log, so a new denial shape is visibly anomalous (a distinct sentinel, never sharing
    `none` with a successful outcome) rather than silently dropped. `unknown` is the 9th
    `_AdmissionReason` value.
- Server: the allocations tool handler records the decision after `admit()` returns (reads
  enqueue-vs-grant from the returned outcome's allocation state).
- Reconciler: `allocation_promotion.promote_pending` and `reap_queue_timeouts` gain an
  optional `metrics: AdmissionMetrics = AdmissionMetrics.disabled()` parameter. At the point
  each candidate is promoted (it holds the `Allocation` row with `created_at`), the sweep
  calls `record_decision` (granted) + `record_wait((now - created_at).total_seconds())`; the
  timeout reaper calls `record_decision` (queue_timeout) per reaped row. The int count return
  is unchanged. The reconciler wires the real `AdmissionMetrics` through `ReconcileConfig`
  into the `promote_pending` / `reap_queue_timeouts` repair closures.

### E — error counter

`kdive_errors_total{error_category}` counts categorized failures **at their origin**, so a
single root cause is counted once — not once per poll. A worker job failure surfaces to the
agent via `jobs.get`/`jobs.list`, which echo the job's `error_category` through
`ToolResponse.from_job` on *every* poll; counting those echoes at the server middleware would
inflate the count by the poll rate. The split:

- **Server** — do **not** add a second server-side counter. Instead extend the existing
  `kdive.mcp.request.errors` counter with an `error_category` label (added when the result
  carries one), so the request surface gets a by-category breakdown of its per-call error
  rate without any new double-counting. This counter already means "tool calls that returned
  an error" (a RED rate signal), and an echoed `jobs.get` failure is a legitimate failed call
  on that surface.
- **Worker** — `kdive.errors` increments once at the job→`FAILED` transition, labeled with
  the resolved `error_category`. The worker fails a job at more than one site (the
  pre-dispatch unmapped-handler path and the `_dispatch` handler-exception path, both calling
  `queue.fail`); the counter hooks the shared fail seam (keyed on the category passed to
  `queue.fail`), not a single branch, so neither path is missed or double-counted.
- **Reconciler** — `kdive.errors` increments under `infrastructure_failure` per repair named
  in a pass's `failures` (§A).

So `kdive_errors_total` is the **backend-origin** failure counter (worker + reconciler) with
no poll inflation; the request-surface by-category error rate lives on
`kdive_mcp_request_errors{tool,error_category}`. Both are honestly named for what one
increment means.

## Testing

- **Cardinality guard** (`tests/observability/`): assert the four new keys are in
  `ALLOWED_LABEL_KEYS`; assert each label's emitted values are a subset of its bounded enum
  (walk `ALL_REPAIR_KINDS`, the state enums, `ErrorCategory`, `_AdmissionReason`); assert no
  identifier key (`project`/`principal`/`object_id`/`secret_ref`) is allowlisted.
- **A**: `record_repairs` adds the right per-kind counts; `ALL_REPAIR_KINDS` == full plan
  names; a failed repair increments `kdive.errors{infrastructure_failure}`.
- **B + D-gauges**: `read_fleet_snapshot` against a seeded DB returns correct counts; the
  gauge callbacks emit from the cache; a stale cache keeps the last snapshot.
- **D**: `classify` maps each `AdmissionOutcome` shape to the right `(outcome, reason)` —
  the enqueue case (`granted=True` + `REQUESTED` → `queued`), PCIe-busy (`ALLOCATION_DENIED`
  + `reason is None` → `pcie`), a validation `CONFIGURATION_ERROR` → `configuration` (not
  `pcie`), and an unmatched shape → `(rejected, unknown)`; the wait histogram records at
  promotion; the counter records at the tool boundary.
- **E**: the worker increments `kdive.errors` once per job→`FAILED` transition at the shared
  `queue.fail` seam (covering both fail sites, not per poll); the reconciler increments under
  `infrastructure_failure` for a failed pass; the server's `kdive.mcp.request.errors` carries
  the `error_category` label on a failing result.
- Instruments render through `render_prometheus` end to end (collect the scrape reader,
  assert the new metric families appear).

## Rollout / rollback

Emit-only; no migration. Rollback is reverting the branch — no persisted state to undo. The
new series are bounded and additive; existing instruments and dashboards are unchanged.
