# Spec: Expanded operational metrics (#610, second cut)

Status: accepted · ADR: [0191](../adr/0191-expanded-operational-metrics-second-cut.md) · Issue: #610

## Goal

Complete the deferred groups from #601/ADR-0190 — F (provider-op RED), G (build pipeline), H
(capture/debug), I (job/queue health) — within the ADR-0090 §4 label allowlist, emit-only and
additive.

## Non-goals

- No new persisted state, schema, or migration — every instrument reads existing rows or
  in-process values.
- No per-object / per-tenant label. Identifiers stay on the log path (ADR-0089). The one new
  deployment-bounded label (`build_host`) is justified in the ADR §1.
- No change to the existing instruments (ADR-0090, ADR-0190) or to the aux `/metrics`
  transport (ADR-0090 §5).

## Instruments

Instrument names use the dotted convention (`kdive.<area>.<name>`), which the Prometheus
renderer (`health/metrics_text.py`) maps to `kdive_<area>_<name>` (histograms gain the OTel
unit suffix, e.g. `_seconds`).

| # | Instrument | Type | Process | Labels |
|---|---|---|---|---|
| F | `kdive.provider.op.duration` (s) | histogram | worker | `provider`, `job_kind`, `outcome` |
| F | `kdive.provider.op.errors` | counter | worker | `provider`, `job_kind` |
| G1 | `kdive.build.phase.duration` (s) | histogram | worker | `build_phase`, `provider`, `outcome` |
| G2 | `kdive.build_host.leases` | observable gauge | reconciler | `build_host` |
| G2 | `kdive.build_host.capacity` | observable gauge | reconciler | `build_host` |
| G3 | `kdive.build_host.reachable` | observable gauge | reconciler | `build_host` |
| H1 | `kdive.vmcore.capture.duration` (s) | histogram | worker | `capture_method`, `provider`, `outcome` |
| H1 | `kdive.vmcore.capture.bytes` | histogram | worker | `capture_method`, `provider` |
| H2 | `kdive.console.bytes` | counter | reconciler | `outcome` |
| H3 | `kdive.debug.session.duration` (s) | histogram | server | `transport`, `outcome` |
| I | `kdive.job.time_to_claim` (s) | histogram | worker | `job_kind` |
| I | `kdive.job.retries` | counter | worker | `job_kind` |

## Labels (allowlist additions)

`ALLOWED_LABEL_KEYS` gains `build_phase`, `capture_method`, `transport`, `build_host`. Bounds:

- `build_phase` → `BuildPhase` = {`provision`, `source_sync`, `configure`, `compile`,
  `modules`, `artifact`} (new enum in `domain/build_phase.py`).
- `capture_method` → `CaptureMethod` (ADR-0049): {`console`, `host_dump`, `gdbstub`, `kdump`};
  vmcore emits {`kdump`, `host_dump`}.
- `transport` → `DebugTransportKind` = {`gdbstub`, `drgn-live`}.
- `build_host` → **deployment-bounded** (the operator-configured `build_hosts` rows), not a
  code enum (ADR §1). The guard test bounds it to the seeded host set.
- `provider` (existing) → `ResourceKind`; `job_kind` (existing) → `JobKind`.

## Design details

### F — provider-op RED

- `WorkerTelemetry` gains the two instruments and reads a `provider_kind` contextvar
  (`jobs/provider_context.py`: `set_provider_kind(value)` / `take_provider_kind()`).
- Each provider-backed handler, where it already resolves the runtime, calls
  `set_provider_kind(binding.kind.value)`. To get the kind alongside the runtime, the resolver
  gains `binding_for_system` / `binding_for_run` returning `ProviderBinding(kind, runtime)`
  (mirroring the existing `binding_for_session`); handlers switch from `runtime_for_*` to the
  binding form where they need the label.
- `WorkerTelemetry.job_span` clears the contextvar on entry; `_record` reads it on close. If
  set, record `provider.op.duration{provider, job_kind, outcome}`, and on `outcome == "error"`
  also `provider.op.errors{provider, job_kind}`. Unset → no provider-op series.
- A handler that raises before resolving the binding leaves the contextvar unset; the job
  still records `kdive_job_duration` (unchanged) and `kdive_errors_total` (ADR-0190 E).

### G1 — build sub-phase timings

- New `domain/build_phase.py` `BuildPhase` StrEnum.
- New `BuildPhaseRecorder` (in `jobs/worker_telemetry.py` or a sibling): built from the worker
  meter, holds the `build.phase.duration` histogram, exposes
  `phase(build_phase, provider) -> contextmanager` timing the block and stamping `outcome`
  on exit (`error` if the block raised). `disabled()` is the no-op default.
- The recorder + provider kind are threaded into `providers/shared/build_host` orchestration
  (`BuildHostOrchestrator.build_workspace` and the transport/ephemeral-VM bring-up in
  `dispatch.py`). Each delineated phase (provision, source_sync, configure, compile, modules,
  artifact) is wrapped. The build is offloaded to a thread (ADR-0181); histogram `record` is
  thread-safe, so the recorder is passed by value and used inside the thread.
- Where a provider's build path does not have a phase (e.g. local-libvirt has no `provision`),
  that phase simply is not recorded — the series exists for the providers that have it.

### G2 + G3 — build-host snapshot

- New `reconciler/build_host_fleet.py`: a `BuildHostSnapshot` dataclass
  (`leases: Mapping[str,int]`, `capacity: Mapping[str,int]`, `reachable: Mapping[str,float]`,
  all keyed by host **name**) and `async read_build_host_snapshot(conn) -> BuildHostSnapshot`:
  - `leases`: `SELECT h.name, count(l.run_id) FROM build_hosts h LEFT JOIN build_host_leases l
    ON l.build_host_id = h.id GROUP BY h.name` (LEFT JOIN so a host with 0 leases still emits).
  - `capacity`: `h.max_concurrent` per host name.
  - `reachable`: `1.0` if `h.state = 'ready'` else `0.0`, per host name.
- `BuildHostTelemetry` (built from the reconciler meter) registers the three observable gauges
  over the cached snapshot; `refresh(snapshot)` swaps the frozen cache; `disabled()` is the
  no-op. Same shape as `FleetTelemetry`.
- `Reconciler._pass_loop` reads the snapshot once per pass (its own pooled connection) and
  calls `refresh`. A read failure logs and leaves the previous cache.

### H1 — vmcore capture

- New `jobs/handlers/capture_telemetry.py` `CaptureTelemetry` (worker meter): the two
  histograms; `record(capture_method, provider, outcome, *, size_bytes=None)`; `disabled()`
  no-op. Injected into the vmcore registrar (built from `metrics.get_meter("kdive.worker")`).
- **Byte source.** `CaptureOutput` (`providers/ports/retrieve.py`) carries no size today
  (`raw`/`redacted` are `StoredArtifact`, which holds only key/etag/sensitivity/retention).
  Add a `raw_size_bytes: int` field to the `CaptureOutput` NamedTuple — an in-process
  port-contract change, no DB/schema/migration impact — set by each provider's `capture` to
  `len(data)` of the raw vmcore it writes (the size is already in hand at write time, e.g.
  `local_libvirt/retrieve.py`; the remote kdump path has it on `CoreInfo.size_bytes`). Every
  `CaptureOutput(...)` construction site (local/remote/fault-inject providers + tests) is
  updated. The handler reads `output.raw_size_bytes`; no extra S3 HEAD round-trip.
- **Provider label.** `capture_handler` today uses `resolver.runtime_for_system` (runtime
  only, no kind). It switches to the **F `binding_for_system`** addition (one binding
  resolution serving both this label and F's contextvar tag) so the `provider` kind is in hand.
- `capture_handler` times `retriever.capture` + `finalize_capture`, and on success records
  duration + `output.raw_size_bytes`; on failure records duration with `outcome=error` and no
  bytes. `capture_method` comes from the job payload `method`.

### H2 — finalized console bytes

- New `ConsoleTelemetry` (reconciler meter): `kdive.console.bytes` counter;
  `record(byte_len)` adds `byte_len` under `outcome=success` when `byte_len > 0`, else `1`-row
  `outcome=empty` with `0` bytes (so an empty finalize is still visible). `disabled()` no-op.
- Wired where the reconciler finalizes console collectors (`reconciler/console` hosting →
  `ConsoleCollector.finalize` / `write_console_artifact`). The collector reports the assembled
  byte length to the telemetry at finalize. No per-System label.
- **Scope.** This finalize path is the **remote-libvirt** console collector
  (`providers/remote_libvirt/console/`). Local-libvirt console uses the separate #117
  etag-refresh artifact flow, which this hook does not cover, so the counter measures remote
  console bytes — not a fleet-wide total. Documented here so a dashboard does not misread it as
  all-provider console volume; extending to local console is a follow-up if local consoles grow
  a comparable finalize seam.

### H3 — debug-session duration

- New `DebugSessionTelemetry` (server meter): `kdive.debug.session.duration` histogram;
  `record(transport, outcome, seconds)`; `disabled()` no-op. Injected into the debug-session
  registrar (`metrics.get_meter("kdive.mcp")`).
- `end_session` computes `now - session.created_at` and records with the session's transport
  and the detach outcome (`ok`/`error`).

### I — job/queue health

- `WorkerTelemetry` gains `kdive.job.time_to_claim` histogram and `kdive.job.retries` counter,
  plus `record_time_to_claim(job_kind, seconds)` and `record_job_retry(job_kind)`.
- `Worker.run_once` computes `(job.heartbeat_at - job.created_at).total_seconds()` after a
  successful `dequeue` (both timestamps are on the claimed row) and records time-to-claim.
- At each worker `queue.fail` site, after the call, if the returned job state is `QUEUED` (a
  requeue, not terminal), record a retry.
- Lease-expiry reclaims: no new instrument — already
  `kdive_reconciler_repairs_total{repair_kind ∈ {abandoned_jobs, reclaimed_build_host_leases}}`.

## Testing

- **Cardinality guard** (`tests/observability/test_label_value_bounds.py`): extend `_BOUNDS`
  with `build_phase`/`capture_method`/`transport`; add the four keys to the allowlist
  assertion; drive every new emitter into the in-memory meter (the existing `_emit_everything`
  helper grows) and assert no identifier key leaks and each enum-bounded value stays within
  its set. `build_host` is asserted against the seeded host names (deployment-bounded), with a
  comment that it is the documented non-enum exception.
- **F**: a provider-tagged job emits `provider.op.duration{provider,job_kind,outcome}` and, on
  error, `provider.op.errors`; an untagged job emits neither; the contextvar is cleared between
  jobs (a tagged job followed by an untagged one does not leak the prior provider).
- **G1**: the recorder times each phase and stamps `error` when the block raises; phases not
  present for a provider are absent.
- **G2/G3**: `read_build_host_snapshot` against a seeded DB returns correct lease/capacity/
  reachable maps (incl. a 0-lease host and an unreachable host); the gauge callbacks emit from
  the cache; a stale cache keeps the last snapshot.
- **H1**: capture success records duration + bytes with the method/provider; failure records
  duration `outcome=error` and no bytes.
- **H2**: a non-empty finalize adds the byte count under `success`; an empty finalize emits
  `empty`; no `system`/identifier label is present.
- **H3**: `end_session` records duration with the session transport and outcome.
- **I**: time-to-claim is recorded from `heartbeat_at - created_at`; a non-terminal
  `queue.fail` increments retries while a terminal one does not.
- **End-to-end render**: collect the scrape reader and assert every new metric family appears
  through `render_prometheus`.

## Rollout / rollback

Emit-only; no migration. Rollback is reverting the branch — no persisted state to undo. New
series are bounded and additive; existing instruments and dashboards are unchanged.
