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
| E | `kdive.errors` | counter | server, worker, reconciler | `error_category` |

## Labels (allowlist additions)

`ALLOWED_LABEL_KEYS` gains `repair_kind`, `state`, `error_category`, `reason`. Bounded by:

- `repair_kind` → `ALL_REPAIR_KINDS` (the full set of `_RepairSpec.name`, ~21).
- `state` → `AllocationState` ∪ `SystemState` ∪ `RunState` ∪ `DebugSessionState`.
- `error_category` → `ErrorCategory` (22).
- `reason` → `_AdmissionReason` = {none, quota, budget, capacity, pcie, affinity,
  queue_timeout}.
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
  - count-by-state for `allocations`, `systems`, `runs`, `debug_sessions`.
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
- `classify` mapping:
  - granted → (`granted`, `none`).
  - `QUOTA_EXCEEDED` → (`rejected|queued`, `quota`) — `queued` when `queueable` and the
    request enqueued, else `rejected`.
  - `ALLOCATION_DENIED` + `reason=budget_exceeded` → (`rejected`, `budget`).
  - `ALLOCATION_DENIED` + `reason=affinity_denied` → (`rejected`, `affinity`).
  - `ALLOCATION_DENIED` host-cap (`reason=at_capacity`) → (`rejected|queued`, `capacity`).
  - `CONFIGURATION_ERROR` PCIe → (`rejected`, `pcie`).
  - queue-timeout reaper → (`rejected`, `queue_timeout`).
- Server: the allocations tool handler records the decision after `admit()` returns. Whether
  the request was enqueued vs hard-denied is read from the returned outcome.
- Reconciler: the promotion sweep records a `granted` decision + `record_wait(now -
  created_at)` per promoted allocation; the queue-timeout sweep records a `queue_timeout`
  rejection. These flow through `ReconcileConfig` like the other configured ports.

### E — error counter

- `kdive.errors` counter created once per process meter.
- Server: `TelemetryMiddleware.on_call_tool` already computes the result's `error_category`;
  it increments the counter with that value (and on the raised-exception path under the most
  specific category available, else `infrastructure_failure`).
- Worker: the per-job dispatch records the counter when the job ends with a non-null
  `error_category`.
- Reconciler: §A failed-pass path.

## Testing

- **Cardinality guard** (`tests/observability/`): assert the four new keys are in
  `ALLOWED_LABEL_KEYS`; assert each label's emitted values are a subset of its bounded enum
  (walk `ALL_REPAIR_KINDS`, the state enums, `ErrorCategory`, `_AdmissionReason`); assert no
  identifier key (`project`/`principal`/`object_id`/`secret_ref`) is allowlisted.
- **A**: `record_repairs` adds the right per-kind counts; `ALL_REPAIR_KINDS` == full plan
  names; a failed repair increments `kdive.errors{infrastructure_failure}`.
- **B + D-gauges**: `read_fleet_snapshot` against a seeded DB returns correct counts; the
  gauge callbacks emit from the cache; a stale cache keeps the last snapshot.
- **D**: `classify` maps each `AdmissionOutcome` shape to the right `(outcome, reason)`;
  the wait histogram records at promotion; the counter records at the tool boundary.
- **E**: the middleware increments per category on a failing result and on a raised
  exception; the worker increments on a failed job.
- Instruments render through `render_prometheus` end to end (collect the scrape reader,
  assert the new metric families appear).

## Rollout / rollback

Emit-only; no migration. Rollback is reverting the branch — no persisted state to undo. The
new series are bounded and additive; existing instruments and dashboards are unchanged.
