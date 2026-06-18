# Diagnostics worker-vantage dispatch (#514)

- **Date:** 2026-06-17
- **Issue:** #514 — `ops.diagnostics`: wire worker-vantage checks (`provider_tls`,
  `gdbstub_acl`) so they run instead of `not_implemented`.
- **ADR:** [ADR-0163](../../adr/0163-diagnostics-worker-vantage-dispatch.md)
- **Builds on:** ADR-0091 (the `Check`/three-state/vantage model), ADR-0125 (the server-vantage
  reachability probe whose connection lifecycle the TLS probe reuses), ADR-0139 (the
  feature-not-enabled vs worker-unavailable substitution honesty this work replaces with real
  dispatch), ADR-0083 (the remote debug client runs worker-side, which fixes the vantage),
  ADR-0079 (`gdb_addr` is the ACL'd security boundary).

## Problem

In a remote-libvirt deployment, the two worker-vantage `ops.diagnostics` checks — `provider_tls`
and `gdbstub_acl` — never run. The default service factory wires no worker-job dispatch, so
`default_service_factory` builds the service with `worker_available=False` and substitutes both
checks with a `FEATURE_NOT_ENABLED` (`not_implemented`) result (ADR-0139). Because the
remote-libvirt debug path depends on the gdbstub being reachable, a permanently-stubbed
`gdbstub_acl` is a coverage gap for exactly the workflow the diagnostic exists to support.

ADR-0139 was the *honesty* slice: it made the substitution detail name its cause and explicitly
deferred the *capability* (actually probing TLS/ACL from a worker job) to this work. The two check
classes (`ProviderTlsCheck` at `checks.py:288`, `GdbstubAclCheck` at `checks.py:340`) already exist
and are tested; what is missing is (a) a worker-job dispatch seam, (b) the two production probe
adapters, and (c) the wiring that lets `default_service_factory` run them for real.

### Why the vantage is load-bearing

ADR-0091 §2 forbids running these from the server: "if a future provider connects the debug
client from elsewhere, this check must move to that vantage rather than silently validating the
wrong hop." The remote debug client runs worker-side (ADR-0083), so a faithful `gdbstub_acl` probe
must execute on the worker — its network path to `gdb_addr` is the one the real debug session
uses, and may differ from the server's. Reclassifying these as server-vantage (like
`remote_libvirt_reachability`) is therefore rejected: it would validate the wrong hop.

## Acceptance criteria (from the issue)

1. In a remote-libvirt deployment with a worker, `gdbstub_acl` and `provider_tls` produce a real
   three-state result instead of `not_implemented`.
2. When no worker is available, the honest substitution is retained (no hang, no fabricated
   `fail`). See "Substitution semantics" for the precise mapping.

## Design

The job queue (`jobs/queue.py`) is the only server→worker handoff in the codebase, so faithful
worker-vantage execution *is* a durable job. `ops.diagnostics` keeps its single-coherent-verdict
contract (ADR-0091 §1) by enqueuing a diagnostics job and **bounded-waiting** for it within the
existing overall deadline.

### 1. New durable job kind

`JobKind.DIAGNOSTICS_WORKER_CHECK = "diagnostics_worker_check"` plus migration
`0040_diagnostics_worker_check_job_kind.sql`, which drops and recreates the `jobs_kind_check`
CHECK constraint with the new value appended (mirrors `0024_image_build_job_kind.sql`). The
SQL↔enum tie is asserted by `tests/db/test_migrate.py:24` (`("jobs_kind_check", models.JobKind)`),
so the migration and enum must stay in lock-step.

Payload `DiagnosticsWorkerCheckPayload{provider: str}`. The handler re-resolves
`remote_config_from_inventory()` at probe time on the worker (config-at-probe-time, exactly as the
reachability probe does), so the payload carries no host identity — minimal cross-process coupling
and no secret material on the queue.

### 2. Worker handler — `jobs/handlers/diagnostics.py`

Resolves the remote-libvirt config, builds `ProviderTlsCheck` + `GdbstubAclCheck` with the
production probes, runs each through `run_check` (per-check timeout, so a black-holing host is an
`error`, never a hang), serializes the resulting `CheckResult`s to a small JSON blob in the object
store, and returns its key as the job's `result_ref`.

- **Config resolution failure** (no/many `[[remote_libvirt]]` instances, malformed inventory)
  raises `CategorizedError(CONFIGURATION_ERROR)` from `remote_config_from_inventory()`; the handler
  lets it propagate so the job dead-letters and the dispatcher maps it to an `error` verdict.
- **Result blob** is operator-config-derived only (provider name, `gdb_addr`, port range, a CA
  label). It carries no tenant data and no secret material; the probes are written to never place
  secret material in `detail`/`fix`. A redaction pass over the serialized blob is applied before it
  is stored, as a defense-in-depth backstop consistent with the redaction invariant.

### 3. Production probe adapters

`diagnostics/provider_tls.py` — `provider_tls_probe()` builds a `TlsProbe`. It opens the
`qemu+tls://` connection over the same `remote_connection` path reachability uses (blocking work
offloaded with `asyncio.to_thread`), with a fresh per-probe `SecretRegistry` for TLS
materialization:
- connection opens and `getInfo()` returns → `TlsProbeOutcome.VALID` (libvirt validated the chain
  against the configured CA) → `pass`.
- libvirt error whose category is `CONFIGURATION_ERROR` arising from cert verification, or a raw
  `libvirt.libvirtError` whose message indicates a TLS/cert verification failure → `INVALID` →
  `fail` (reissue / set `KDIVE_PROVIDER_CA`).
- `TRANSPORT_FAILURE` (TLS connect failed, host down/port closed) → `UNREACHABLE` → `error`.

`diagnostics/gdbstub_acl.py` — `gdbstub_acl_probe()` builds a `GdbstubAclProbe`. A policy check, no
live listener (ADR-0091 §2): it attempts a TCP connect from the worker to `host:port` for the
lowest port in the configured range (one representative port — the ACL admits a range, not a single
port), with a short connect timeout:
- connect succeeds, **or** is refused fast (`ECONNREFUSED` — port reachable, nothing listening) →
  `True` (ACL admits) → `pass`.
- connect times out (firewall `DROP`) → `False` (blocked) → `fail` (open the host firewall/ACL).
- any other error (DNS failure, unexpected `OSError`) → `None` (indeterminate) → `error`.

`gdb_addr` is required for the probe; when the resolved config has `gdb_addr is None` the handler
reports `gdbstub_acl` as `error` (cannot probe an unset address) rather than guessing.

Both probes are injected callables (`TlsProbe`, `GdbstubAclProbe`) so the handler and the checks
are unit-tested against fakes for all three outcomes without live hardware — the established
pattern for the reachability and base-image-staging probes.

### 4. Server-side bounded-wait dispatcher — `diagnostics/worker_dispatch.py`

`WorkerCheckDispatcher` is a one-method port:

```python
async def run_worker_checks(
    self, provider: str | None, *, deadline: float | None
) -> list[CheckResult]: ...
```

The production `JobWorkerCheckDispatcher` captures the pool + object store. It:

1. Enqueues a `DIAGNOSTICS_WORKER_CHECK` job with a **per-call unique `dedup_key`**
   (`f"diagnostics:{provider}:{uuid4}"`) — no single-flight, because (unlike the egress probe)
   these are cheap reads, so two concurrent `doctor` runs harmlessly enqueue two jobs.
2. Polls `get_by_dedup_key` on a short interval until the job reaches a terminal state or the
   remaining deadline elapses.
3. On **succeeded** → reads + deserializes the `result_ref` blob into `CheckResult`s.
4. On **failed** → returns one `error` `CheckResult` per worker-vantage check, carrying the job's
   `error_category` (e.g. `configuration_error` for a bad inventory).
5. On **deadline reached with the job still queued/running** → returns `WORKER_UNAVAILABLE`
   substitutions (`transport_failure`, the `/livez`/`/readyz` detail) — the exact signal ADR-0139
   reserved for "dispatch exists but the worker cannot pick the job up." Never a hang.

The dispatcher owns the full worker-vantage outcome (run or substitute), keeping
`DiagnosticsService.run()` a simple merge.

### 5. Service + factory wiring

`DiagnosticsService` gains an optional `worker_dispatcher: WorkerCheckDispatcher | None` and a list
of worker-vantage check specs (the `(check_id, provider)` pairs to dispatch). When a dispatcher is
present it is called with the remaining overall budget and its results are merged into the report;
the legacy `unavailable_worker_checks` + `substitution_reason` path is **only** used when
`worker_dispatcher is None`.

`default_service_factory` constructs the production `JobWorkerCheckDispatcher` (pool + store) and
passes it when `is_remote_libvirt_configured()`. The factory therefore needs the pool and an object
store; `ops.diagnostics.register` already has the pool, and resolves the store the same way the
other ops tools do (`object_store_from_env`, degrading to a `None` dispatcher when S3 is
unconfigured so the service falls back to the honest substitution rather than crashing).

### Substitution semantics (acceptance criterion 2)

| Deployment state | worker-vantage result |
|---|---|
| No dispatch wired (`worker_dispatcher is None`: S3 unconfigured, or remote-libvirt not configured) | `FEATURE_NOT_ENABLED` → `not_implemented` (unchanged) |
| Dispatch wired, worker runs the job | real `pass`/`fail`/`error` from the probes |
| Dispatch wired, job dead-letters (bad config) | `error` with the job's `error_category` |
| Dispatch wired, worker never picks the job up within the deadline | `WORKER_UNAVAILABLE` → `transport_failure`, "check /livez/readyz" |

This honors the AC's intent — no hang, no fabricated `fail`, an honest three-state `error` when the
check cannot run — and uses ADR-0139's `WORKER_UNAVAILABLE` for the genuine worker-down case (the
exact condition that ADR built the signal for). `not_implemented` is retained verbatim for
deployments that wire no dispatch.

## Testing

- **Probe units** — each probe maps its three observable conditions to the right outcome, with a
  fake connection/socket: TLS valid/invalid/unreachable; ACL admit (connect + refused-fast) /
  blocked (timeout) / indeterminate; `gdb_addr is None`.
- **Handler unit** — builds both checks, runs them, serializes/round-trips the blob; a
  config-resolution failure propagates (job dead-letters); the blob carries no secret material.
- **Dispatcher unit** — fake pool/store + a seeded job row: succeeded → real results; failed →
  `error` with category; deadline-with-no-pickup → `WORKER_UNAVAILABLE`. No real DB needed beyond
  the existing queue test harness; a DB-backed test exercises enqueue→poll once.
- **Service unit** — with a fake dispatcher returning a fixed list, the report merges
  server-vantage + worker-vantage results; with `worker_dispatcher=None`, the legacy substitution
  is unchanged.
- **Default-factory unit** — remote-libvirt configured + store present → a dispatcher-backed
  service; store absent → `None` dispatcher → substitution retained.
- **Migration** — `test_migrate.py` already asserts the `jobs_kind_check`↔`JobKind` tie; the new
  value flows through it.
- **No live-hardware test.** Like reachability and base-image-staging, the live worker→host probe
  is hardware-gated; a runbook/OPERATOR-TODO records the live verification.

## Out of scope

- The `guest_egress` probe (separate opt-in, ADR-0091 §3) is untouched.
- Single-flighting / dedup of concurrent diagnostics jobs (cheap reads; not needed).
- Threading an allocation→resource→instance identity for multi-instance remote-libvirt (the whole
  diagnostics path already assumes the single declared instance, per `_resolve_instance`).
