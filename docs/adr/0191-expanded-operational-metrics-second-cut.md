# ADR 0191 — Expanded operational metrics, second cut (provider ops, build pipeline, capture/debug, job health)

- **Status:** Accepted
- **Date:** 2026-06-19
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0190](0190-expanded-operational-metrics.md)
  (the #601 first cut A/B/D/E this completes), [ADR-0090](0090-opentelemetry-adoption-service-health.md)
  (the per-process aux `/metrics` listener + §4 label allowlist), [ADR-0189](0189-bundled-prometheus-metrics-collection.md)
  (the Prometheus that collects these series), [ADR-0019](0019-tool-response-envelope.md) (the error taxonomy).
- **Spec:** [`../design/expanded-operational-metrics-second-cut.md`](../design/expanded-operational-metrics-second-cut.md)
- **Related:** #610 (this), #601/ADR-0190 (the first cut), #600 (the collector), #359/ADR-0103
  (the build-host reachability probe), #099/ADR-0099 (the build-host lease model).

## Context

ADR-0190 shipped the first cut (groups A reconciler-repairs, B lifecycle-inventory, D
admission/capacity, E error-taxonomy) and **explicitly deferred** four groups until the
first-cut labels and dashboards settled (#601 → #610):

- **F. Provider ops (RED).** Per-provider duration + error rate for the long provider
  operations (provision/build/install/boot/capture). Today `kdive_job_duration{job_kind,outcome}`
  measures every worker job but carries **no provider dimension**, so a slow `local-libvirt`
  build is indistinguishable from a slow `remote-libvirt` one.
- **G. Build pipeline.** Sub-phase timings (ephemeral-VM provision vs source sync vs
  compile), active build-host leases per host, and build-host reachability — the #359
  reconciler probe writes `build_hosts.state` but nothing graphs it.
- **H. Capture / debug.** vmcore capture duration + bytes, finalized console bytes, and
  gdbstub/drgn session duration.
- **I. Job/queue health.** Queue latency (enqueue→claim, distinct from handler duration)
  and job retries.

The governing constraint is unchanged: the ADR-0090 §4 label allowlist
(`observability/labels.py` `ALLOWED_LABEL_KEYS`). A metric label may carry only a reviewed,
low-cardinality key bounded by an enum; high-cardinality identity (`project`, `principal`,
`object_id`, `secret_ref`) travels on the access-controlled log path (ADR-0089), never as a
metric label. The cardinality-guard test (`tests/observability/test_label_value_bounds.py`)
is the enforcement and is extended for every new label.

## Decision

### 1. New labels (allowlist additions)

Add four keys to `ALLOWED_LABEL_KEYS`. `provider` (= `ResourceKind`) and `job_kind`
(= `JobKind`) are already allowlisted and are reused for group F.

| Key | Bounded by | Cardinality | Used by |
|---|---|---|---|
| `build_phase` | `BuildPhase` enum (new) | 6 | G build sub-phase timings |
| `capture_method` | `CaptureMethod` (ADR-0049) | 4 | H vmcore capture |
| `transport` | `DebugTransportKind` literal {`gdbstub`,`drgn-live`} | 2 | H debug-session duration |
| `build_host` | **the operator-configured `build_hosts` set** (deployment-bounded) | small, static | G lease / reachability gauges |

`build_host` is a **deliberate, scoped exception** to the "enum-bounded" rule, and is the only
new key not pinned to a code enum:

- Build hosts are a **small, static, operator-configured fleet** (the `build_hosts` table — a
  handful of rows, e.g. the `[[remote_libvirt]]`/SSH hosts an operator declares), not a
  per-tenant or per-run dimension. Cardinality is bounded by operator configuration, the same
  shape as `provider`.
- The aux `/metrics` endpoint is **operator-scoped** (ADR-0090 §5, never re-exposed off the
  network boundary, ADR-0189), and a build-host *name* is an operator-chosen infrastructure
  label — not tenant reconnaissance data the way `project`/`principal` are (ADR-0089). The
  per-host breakdown is the operating signal an operator needs to spot one wedged or
  saturated build host.
- The cardinality-guard test bounds `build_host` to **the seeded `build_hosts` rows** for the
  pass under test (operator config), not a code enum, and a comment records why this key is
  exempt from the enum rule. A new build host appears as a new series only when an operator
  adds a row — never on a tenant action.

### 2. F — provider-op RED (`kdive.worker`)

Two new instruments, recorded **once per provider-backed job** at the worker dispatch
boundary (`WorkerTelemetry`):

- `kdive_provider_op_duration_seconds` histogram, labels `{provider, job_kind, outcome}`.
- `kdive_provider_op_errors_total` counter, labels `{provider, job_kind}` (the failed subset;
  `outcome` is definitionally `error` here, so it is omitted — the duration histogram's
  `outcome` label carries the RED success/error split).

The provider dimension is the only thing F adds over the existing `kdive_job_duration`, so it
is recorded at the **same** dispatch boundary rather than as a redundant second timer. The
handler knows the provider kind (it resolves the runtime); it tags the in-flight job via a
`provider_kind` **contextvar** (`jobs/provider_context.py`), and the worker's per-job
`_record` reads and clears it. A job with no provider binding (a pure-DB job kind) leaves the
contextvar unset and emits no provider-op series — only `kdive_job_duration`, unchanged. The
contextvar mirrors the codebase's existing `bind_context` logging contextvar; it avoids
threading a span handle through every `(conn, job)` handler signature.

### 3. G — build pipeline

**G1 — sub-phase timings (`kdive.worker`).** `kdive_build_phase_duration_seconds` histogram,
labels `{build_phase, provider, outcome}`. A `BuildPhase` enum names the orchestrator's real
sub-phases: `provision` (transport/ephemeral-VM bring-up), `source_sync` (git clone /
warm-tree rsync), `configure` (`olddefconfig` + config read + preflight), `compile` (`make`),
`modules` (`modules_install` + bundle), `artifact` (build-id / vmlinux extraction). A
`BuildPhaseRecorder` (from the worker meter) is threaded into the shared build orchestrator
(`providers/shared/build_host`) and times each phase; the build runs offloaded in a thread
(ADR-0181), and OTel histogram `record` is thread-safe. The recorder defaults to a no-op so
the non-instrumented path is unconditional.

**G2 — build-host leases + capacity (`kdive.reconciler`).** Observable gauges
`kdive_build_host_leases{build_host}` (active leases per host) and
`kdive_build_host_capacity{build_host}` (advertised `max_concurrent` per host), emitted from a
per-pass `BuildHostSnapshot` cached on a `BuildHostTelemetry` — the same per-pass-cache /
sync-callback pattern as ADR-0190's `FleetSnapshot` (an OTel observable callback is
synchronous and cannot await the async pool). A snapshot read failure leaves the previous
cache (best-effort, like a repair).

**G3 — build-host reachability (`kdive.reconciler`).** Observable gauge
`kdive_build_host_reachable{build_host}` = `1.0` when `build_hosts.state = 'ready'`, `0.0`
when `'unreachable'`, from the same `BuildHostSnapshot`. The state transition *count* is
already covered by `kdive_reconciler_repairs_total{repair_kind="build_host_states_changed"}`
(ADR-0190 A); this gauge adds the current per-host up/down level.

### 4. H — capture / debug

**H1 — vmcore capture (`kdive.worker`).** `kdive_vmcore_capture_duration_seconds` histogram
`{capture_method, provider, outcome}` and `kdive_vmcore_capture_bytes` histogram
`{capture_method, provider}` (raw vmcore size), recorded in `capture_handler` around
`retriever.capture` + finalize. `capture_method` ∈ {`kdump`, `host_dump`} for vmcore;
`bytes` is recorded only on success. The byte size is not on `CaptureOutput` today, so a
`raw_size_bytes: int` field is added to that NamedTuple (in-process port-contract change, no
DB/migration impact) and set by each provider to `len(data)` of the raw vmcore it writes; the
handler reads `output.raw_size_bytes` rather than issuing an extra object HEAD. The `provider`
label reuses the F `binding_for_system` resolver addition.

**H2 — finalized console bytes (`kdive.reconciler`).** `kdive_console_bytes_total` **counter**,
label `{outcome}` ∈ {`success`, `empty`} — **no per-System label**. The issue's "per System"
framing is a per-object label, which the allowlist forbids; the honest aggregate is total
console bytes finalized, split only by whether the finalized stream had content. Incremented at
console finalization (`ConsoleCollector.finalize` → `write_console_artifact`) with the
assembled byte length. The finalize seam is the **remote-libvirt** console collector; local
console (the #117 etag-refresh flow) is not covered, so the counter is remote-console bytes,
not a fleet total — documented so it is not misread.

**H3 — debug-session duration (`kdive.mcp`).** `kdive_debug_session_duration_seconds`
histogram `{transport, outcome}`, recorded at `end_session` from
`now - session.created_at`. The live session **count** is already
`kdive_debug_sessions{state}` (ADR-0190 B), so only duration is added here.

### 5. I — job/queue health (`kdive.worker`)

- `kdive_job_time_to_claim_seconds` histogram, label `{job_kind}` — `heartbeat_at -
  created_at` (claim time − enqueue time), recorded in `Worker.run_once` after a successful
  `dequeue`. Distinct from `kdive_job_duration` (handler wall-clock): this is queue latency.
- `kdive_job_retries_total` counter, label `{job_kind}` — incremented once per **non-terminal**
  `queue.fail` (a requeue, `state` returns to `QUEUED`), at the worker's fail sites. A terminal
  `FAILED` is not a retry (it is counted by `kdive_errors_total`, ADR-0190 E).
- **Lease-expiry reclaims** are already `kdive_reconciler_repairs_total{repair_kind ∈
  {abandoned_jobs, reclaimed_build_host_leases}}` (ADR-0190 A). No new instrument; documented
  so the overlap is explicit.

### 6. No schema / migration change

Every instrument reads existing rows or in-process state; nothing is persisted. Metrics emit
on the existing per-process aux `/metrics` and are aggregated by the ADR-0189 Prometheus.

## Consequences

- The reconciler gains a second per-pass read (build-host lease counts + states, a small
  grouped `COUNT(*)` + a `build_hosts` scan), independent of scrape rate — the same cost model
  ADR-0190 already accepted for `FleetSnapshot`.
- New series are bounded: provider-op ≤ |providers|×|job_kinds|×2; build-phase ≤
  6×|providers|×2; build-host gauges 3×|build_hosts| (operator-bounded); vmcore 2×|providers|;
  console ≤2; debug-session 2×2; job health ≤|job_kinds|. No per-tenant/per-run label.
- `build_host` is the first deployment-bounded (non-enum) allowlist key; the guard test and a
  module comment record the exemption so it is not cargo-culted into a tenant-identity leak.
- Dashboards can break provider operations down by provider, watch each build sub-phase, see
  per-host build capacity/health, and split queue latency from handler time.

## Considered & rejected

- **A new `op` enum label for group F** — duplicates the `JobKind` value set; `job_kind` is
  already allowlisted and already names the operation. Reuse it.
- **Adding `provider` to the existing `kdive_job_duration`** instead of a new instrument —
  changes an existing series' cardinality and meaning, and would still need to skip
  no-provider jobs; a separate, clearly-named provider-op pair is honest about what it counts.
- **`kdive_console_bytes_total{system}` (the issue's literal "per System")** — a per-object
  label, the exact cardinality/reconnaissance footgun the allowlist exists to prevent. The
  aggregate counter with an `outcome` split is the compliant form.
- **Aggregate-only build-host metrics (no `build_host` label)** — loses the per-host signal an
  operator needs to spot one wedged host; build hosts are a small operator-configured fleet,
  so a deployment-bounded label is justified (§1) rather than dropped.
- **A new dedicated session-count gauge / lease-reclaim counter** — both already exist
  (`kdive_debug_sessions{state}`, `kdive_reconciler_repairs_total`); duplicating them would
  double-count.
- **Threading a span/telemetry handle through every `(conn, job)` handler for group F** — a
  broad signature change; a single `provider_kind` contextvar set where the handler already
  resolves the binding is the minimal seam.

## Related

- ADR-0190 (#601 first cut), ADR-0090 (§4 allowlist, §5 aux listener), ADR-0089 (identity on
  the log path), ADR-0049 (capture-method vocabulary), ADR-0103/#359 (build-host reachability
  probe), ADR-0099 (build-host leases), ADR-0181 (offloaded build), ADR-0189/#600 (the
  Prometheus that collects these).
