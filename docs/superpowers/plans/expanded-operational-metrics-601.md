# Plan: Expanded operational metrics (#601, first cut)

Spec: [`../../design/expanded-operational-metrics.md`](../../design/expanded-operational-metrics.md) ·
ADR: [`../../adr/0190-expanded-operational-metrics.md`](../../adr/0190-expanded-operational-metrics.md)

Execution mode: **direct, sequential, in-session** — the tasks are tightly coupled
(`reconciler/loop.py`, `ReconcileConfig`, `__main__.py` reconciler/worker wiring, and the
shared label allowlist are touched by several tasks), so parallel subagents would collide.
Each task is TDD: failing test first, minimal impl, focused guardrails, then refactor green.

Guardrail commands (CI gates each individually — run the relevant ones before every commit):
`just lint` · `just type` (whole tree) · `just test` · plus `just docs-check` /
`config-guard` only if generated docs or config change (they should not here).
Single test: `uv run python -m pytest <path>::<name> -q`.

## Conventions every task follows

- Telemetry classes follow the `WorkerTelemetry`/`ReconcilerTelemetry` pattern: built from a
  `meter` (+ `tracer` where spans are involved), a `.disabled()` no-op factory via
  `cls.__new__(cls)` setting `_enabled = False`, and `if self._enabled:` guards on every emit
  so the hot path is unconditional.
- Instrument names are dotted (`kdive.<area>.<name>`); the Prometheus renderer maps to
  underscores. Labels carry only allowlisted keys with bounded-enum values.
- Return the project's structured types; never invent label strings — derive from enums.

## Task 1 — allowlist + cardinality guard (foundation)

**Fits:** the §1 allowlist additions that every other task's labels depend on.
**Files:** `src/kdive/observability/labels.py`; `tests/observability/test_label_allowlist.py`
(extend), new `tests/observability/test_label_value_bounds.py`.
**Do:**
- Add `repair_kind`, `state`, `error_category`, `reason` to `ALLOWED_LABEL_KEYS`.
- New test asserts: the four keys are present; the known identifier keys remain absent
  (existing test already covers this — keep it). A value-bounds test that imports the bounded
  sets used by later tasks is added incrementally as those constants land (Tasks 2/4) — in
  this task, assert only the key membership so the test is self-contained now.
**Acceptance:** `test_label_allowlist.py` green; `ALLOWED_LABEL_KEYS` has the 4 new keys; no
identifier key added. `just lint type` and the observability test dir green.
**Rollback:** revert the labels.py line.

## Task 2 — reconciler repairs counter (A)

**Fits:** §2; fills the reconciler's repair-visibility gap.
**Files:** `src/kdive/reconciler/loop_telemetry.py` (extend `ReconcilerTelemetry`);
`src/kdive/reconciler/loop.py` (call `record_repairs` in `_pass_loop`; declare
`ALL_REPAIR_KINDS`); `tests/reconciler/test_loop_telemetry.py` (or the existing telemetry
test) + a test that `_repair_plan` with all optional ports enabled == `ALL_REPAIR_KINDS`.
**Do (TDD):**
1. Failing test: a `ReconcilerTelemetry` built on an `InMemoryMetricReader` meter, after
   `record_repairs({"orphaned_systems": 2, "promoted_allocations": 0, ...}, failures=[])`,
   emits `kdive_reconciler_repairs_total{repair_kind="orphaned_systems"} == 2` and a `0`
   series for a zero kind. A second test: `record_repairs(..., failures=["leaked_domains"])`
   increments `kdive_errors_total{error_category="infrastructure_failure"}` by 1.
2. Failing test: `set(name for name in ALL_REPAIR_KINDS)` equals the names produced by
   `_repair_plan` built with every optional config port supplied (stub ports).
3. Impl: add the `kdive.reconciler.repairs` counter + `kdive.errors` counter to
   `ReconcilerTelemetry`; `record_repairs(counts, failures)` adds each count (incl. 0) under
   `repair_kind`, and one `kdive.errors{infrastructure_failure}` per failure name. Declare
   `ALL_REPAIR_KINDS` near `_repair_plan`. **Thread the raw `counts` dict (keys =
   `_RepairSpec.name`) out of `reconcile_once`** — extend `ReconcileReport` with a
   `repair_counts: Mapping[str, int]` field (or return the dict alongside) so `_pass_loop`
   passes spec-named counts straight to `record_repairs`. Do **not** reconstruct counts from
   the existing `ReconcileReport` scalar fields: their names diverge from the spec names
   (e.g. the field `reconciled_inventory` vs the spec name `reconcile_inventory`), which would
   make `repair_kind` labels mismatch `ALL_REPAIR_KINDS` and fail step 2's test.
**Acceptance:** both tests green; `kdive.errors` counter exists on the reconciler meter;
`ALL_REPAIR_KINDS` pinned to the plan. `just test` for `tests/reconciler/` + `just type`.
**Rollback:** revert loop_telemetry + loop.py hunks; counter is additive.

## Task 3 — fleet snapshot gauges (B + D-gauges)

**Fits:** §3 + §4 gauges; the instant fleet inventory + host capacity.
**Files:** new `src/kdive/reconciler/fleet.py` (`FleetSnapshot`, `read_fleet_snapshot`);
`src/kdive/reconciler/loop_telemetry.py` or a sibling `fleet_telemetry.py` (`FleetTelemetry`
with the observable gauges + cache + `disabled()`); `src/kdive/reconciler/loop.py` (refresh
per pass); `src/kdive/__main__.py` (construct `FleetTelemetry`, pass via `ReconcileConfig`);
`tests/reconciler/test_fleet.py`.
**Do (TDD):**
1. Failing test (db-marked, testcontainers): seed allocations/systems/runs/debug_sessions in
   assorted states + resources of two providers with caps; `read_fleet_snapshot(conn)` returns
   correct count-by-state (with **every** enum state present, zero-filled) and
   used/total capacity per provider.
2. Failing test (no DB): a `FleetTelemetry` on an in-memory reader, after `refresh(snapshot)`,
   has gauge callbacks emitting `kdive_allocations{state=...}` etc. and
   `kdive_host_capacity_used/total{provider=...}`; before any refresh emits nothing (or all
   zeros); a second `refresh` failure path keeps the last snapshot.
3. Impl: `read_fleet_snapshot` runs the grouped count-by-state queries and the
   occupying-count grouped query, then sums the typed caps in Python (see below);
   `FleetTelemetry` registers observable gauges reading the cached snapshot; `_pass_loop`
   reads a snapshot on its own connection each pass and calls `refresh`, logging+keeping-last
   on failure.

**Host-capacity detail:** `concurrent_allocation_cap` is **not** a column — it lives in the
`resources.capabilities` JSONB, read via the typed `require_allocation_cap` view
(`resource_capabilities.py`), which fails closed on an absent/invalid cap. Load resources and
sum the typed caps **in Python** per provider; skip (and log once) a resource whose cap is
absent/invalid rather than counting it as 0 in a way that hides a misconfig. A plain SQL `SUM`
over a non-existent column is wrong.

**Cross-thread cache:** `FleetSnapshot` is a **frozen** dataclass; `refresh(snapshot)` rebinds
the cache to that one new reference and never mutates fields in place, so a scrape-thread
gauge callback reading the reference under the GIL can never see a half-updated snapshot — the
same single-assignment cross-thread pattern as `WorkerTelemetry.observe_queue_depth`.
**Acceptance:** tests green; gauges render through `render_prometheus`. `just test` for
`tests/reconciler/` + `just type`. Note the db-marked test skips without Docker.
**Rollback:** delete fleet.py + FleetTelemetry; remove the `_pass_loop` refresh + `__main__`
wiring (all additive).

## Task 4 — admission metrics (D counter + wait histogram)

**Fits:** §4; the admission visibility #561 wants.
**Files:** new `src/kdive/services/allocation/admission/metrics.py` (`_AdmissionReason`,
`classify`, `AdmissionMetrics`); `src/kdive/services/allocation/promotion.py` +
`src/kdive/reconciler/repairs/allocations.py` (optional `metrics` param threaded through);
the allocations request handler `src/kdive/mcp/tools/lifecycle/allocations/request.py` (record
after `admit()`, with `AdmissionMetrics` injected via
`src/kdive/mcp/tools/lifecycle/allocations/registrar.py`); `src/kdive/reconciler/loop.py` +
`ReconcileConfig` + `__main__.py` (wire the reconciler's `AdmissionMetrics`);
`tests/services/allocation/test_admission_metrics.py`,
`tests/mcp/test_allocations_tools.py` (counter at the tool boundary), promotion test.
**Do (TDD):**
1. Failing pure-unit tests for `classify(outcome)` over every shape: granted (GRANTED→
   granted/none), enqueue (granted=True+REQUESTED→queued/none), QUOTA_EXCEEDED→rejected/quota,
   ALLOCATION_DENIED+budget_exceeded→budget, +affinity_denied→affinity, +at_capacity→capacity,
   +reason=None→pcie, CONFIGURATION_ERROR→configuration, queue_timeout→queue_timeout,
   an unmatched shape→rejected/unknown.
2. Failing test: `AdmissionMetrics` on an in-memory reader increments
   `kdive_allocation_admission_total{outcome,reason}` per `record_decision` and records
   `kdive_allocation_wait_seconds` per `record_wait`.
3. Failing test: the allocations tool handler, given an injected `AdmissionMetrics`, records
   one decision matching the `admit()` outcome; the promotion sweep records granted+wait per
   promoted row and queue_timeout per reaped row.
4. Impl: `classify` pure mapping; `AdmissionMetrics` emitter + `disabled()`; thread optional
   `metrics` param into `promote_pending`/`reap_queue_timeouts` (default disabled), record in
   the per-candidate loop; record at the tool boundary; wire the real emitter through
   `ReconcileConfig` and the server app.
**Acceptance:** all D tests green; no label outside the allowlist; `outcome` values bounded to
{granted,rejected,queued}. `just test` for the touched dirs + `just type`.
**Rollback:** delete metrics.py; revert the optional-param threads (defaults make them inert)
and the wiring.

## Task 5 — error counter server label (E server side)

**Fits:** §5 server side (worker/reconciler `kdive.errors` already added in Tasks 2 + worker
below).
**Files:** `src/kdive/mcp/middleware/telemetry.py` (add `error_category` to the existing
`kdive.mcp.request.errors` increments); `src/kdive/jobs/worker.py` + `worker_telemetry.py`
(worker `kdive.errors` at the shared `queue.fail` seam);
`tests/mcp/middleware/test_telemetry.py`, `tests/jobs/test_worker_telemetry.py`.
**Do (TDD):**
1. Failing test: the middleware, on a result carrying `error_category`, increments
   `kdive_mcp_request_errors{tool,outcome,error_category}`; on the raised-exception path it
   labels `error_category` with the most specific available category (else
   `infrastructure_failure`).
2. Failing test: the worker increments `kdive_errors_total{error_category}` once per
   job→FAILED at the shared fail seam (cover both the pre-dispatch and handler-exception fail
   sites), not per subsequent poll.
3. Impl: add the `error_category` label to the middleware error counter; add a
   `kdive.errors` counter to `WorkerTelemetry` + a `record_job_failure(category)` hook called
   at the worker's `queue.fail` site(s).
**Acceptance:** tests green; no double-count across worker+server under one name (the worker
counter and the request-error counter are distinct names). `just test` for the touched dirs +
`just type`.
**Rollback:** revert the middleware label add + worker hook (additive).

## Task 6 — end-to-end render + cardinality value-bounds + follow-up issue

**Files:** `tests/observability/test_label_value_bounds.py` (now that `ALL_REPAIR_KINDS`,
`_AdmissionReason`, the state enums, and `ErrorCategory` exist, assert every emitted label
value is in its bounded set); a `render_prometheus` smoke test collecting a reconciler-style
reader and asserting the new metric families appear.
**Also:** file the deferred-scope follow-up GitHub issue (groups F/G/H/I) and reference it
from the PR body.
**Acceptance:** full `just test` + `just type` + `just lint` green; new metric families
render.

## Cross-task rebase/conflict zones

`src/kdive/__main__.py` (reconciler + worker wiring), `src/kdive/reconciler/loop.py` /
`ReconcileConfig`, and `src/kdive/observability/labels.py` are touched by multiple tasks —
implement sequentially in this session so there is no cross-agent contention. Run the full
suite once before pushing (Task 6) since architecture/doc-gen tests live outside the dirs
edited.
