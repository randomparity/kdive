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
`error`, never a hang), serializes the resulting `CheckResult`s to a compact JSON string, and
returns that string **inline** as the job's `result_ref`.

`result_ref` is a nullable `text` column with no "is an object-store key" enforcement
(`domain/models.py:372`), and the diagnostics dispatcher is the *only* reader of this job kind's
result, so the verdict rides inline rather than as an object reference. This is a deliberate,
scoped exception to the usual "`result_ref` is an object key" convention: it avoids a per-run
object-store blob that nothing reaps (every `doctor` run uses a unique `dedup_key`, so a blob per
run would orphan unboundedly — there is no diagnostics-blob reaper), and it keeps the object store
out of the server-side `ops.diagnostics` path entirely. The payload is small (two `CheckResult`s)
and bounded.

- **Config resolution failure** (no/many `[[remote_libvirt]]` instances, malformed inventory)
  raises `CategorizedError(CONFIGURATION_ERROR)` from `remote_config_from_inventory()`; the handler
  lets it propagate so the job dead-letters and the dispatcher maps it to an `error` verdict.
- **No secret material on the wire.** The inline result is operator-config-derived only (provider
  name, `gdb_addr`, port range, a CA label) — no tenant data, no secret material. The probes are
  written to never place secret material in `detail`/`fix`, asserted by a test; this is the actual
  control (the result is not a secret-bearing surface, so no op-scoped redactor is relied on).

### 3. Production probe adapters

`diagnostics/provider_tls.py` — `provider_tls_probe()` builds a `TlsProbe`. It does **not**
piggy-back on libvirt's connection open: a qemu+tls open failure is wrapped opaquely as
`TRANSPORT_FAILURE` by `remote_connection` (`transport.py`), so a cert-verification failure is
indistinguishable from host-down there — and the check's whole value is telling those two apart.
Instead the probe does a direct TLS handshake (Python `ssl`) to the libvirt TLS endpoint using the
materialized client cert/key and the configured CA, classifying via `ssl` exceptions. The endpoint
host and port are **parsed from `config.uri`** (`urlsplit(...).hostname` / `.port`), defaulting the
port to libvirt's TLS default `16514` only when the URI omits it — so the probe targets the exact
endpoint the real worker connection uses, not a hardcoded port that a non-default deployment would
false-`UNREACHABLE`. The outcomes:
- handshake completes → `TlsProbeOutcome.VALID` → `pass`.
- `ssl.SSLCertVerificationError` (server cert not signed by the configured CA, or otherwise fails
  verification) → `INVALID` → `fail` (reissue / set the provider CA).
- `ConnectionRefusedError` / `socket.timeout` / `OSError` (host down, port closed, network drop) →
  `UNREACHABLE` → `error`.
- any other `ssl.SSLError` that is **not** a verification failure (protocol mismatch, etc.) →
  `UNREACHABLE` → `error` (the safe direction: an ambiguous handshake failure is reported as
  "could not validate," never a fabricated `fail`).

Classifying on the typed `ssl` exception (not a libvirt error string) keeps the verdict stable
across libvirt versions; a unit test pins the cert-verification shape so a future regression fails
CI. The TLS materialization reuses the transport's secret-ref → pkipath path; blocking work is
offloaded with `asyncio.to_thread`.

This check is scoped to **chain validity** — that the configured CA signs the server cert and the
client identity presents. It does **not** cover libvirt's application-layer `tls_allowed_dn_list`
(an allow-listed client DN is authz the handshake completes regardless of), which surfaces via the
reachability check / at provision time; a green `provider_tls` means "the TLS chain validates," not
"the worker is fully authorized to connect."

`diagnostics/gdbstub_acl.py` — `gdbstub_acl_probe()` builds a `GdbstubAclProbe`. A policy check, no
live listener (ADR-0091 §2): it attempts a TCP connect from the worker to `host:port` for the
lowest port in the configured range (one representative port — the ACL admits a range, not a single
port), with a short connect timeout:
- connect succeeds, **or** is refused fast (`ECONNREFUSED`) → `True` → `pass`. A fast refusal proves
  the SYN reached the host's TCP stack, which excludes the M2 fault (a path-level `DROP`/blackhole).
- connect times out (firewall `DROP`/blackhole) → `False` (blocked) → `fail` (open the host
  firewall/ACL).
- any other error (DNS failure, unexpected `OSError`) → `None` (indeterminate) → `error`.

**Known limitation:** the fast-refusal signal cannot distinguish "no listener" from an iptables
`-j REJECT` rule (both return `ECONNREFUSED` promptly), so a REJECT-style block reads as `pass`.
The check catches the M2 fault class (a `DROP`/blackholed range, the observed failure) and the
common no-rule-at-all case; it does not catch a deliberate REJECT. The `pass` detail is therefore
worded "host TCP stack reachable on the gdbstub range" rather than asserting the ACL fully admits
it, and the ADR records this as an accepted limitation.

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

The production `JobWorkerCheckDispatcher` captures the pool only (no object store — the result is
inline). It:

1. Enqueues a `DIAGNOSTICS_WORKER_CHECK` job with a **per-call unique `dedup_key`**
   (`f"diagnostics:{provider}:{uuid4}"`) and **`max_attempts=1`** — no single-flight (unlike the
   egress probe, these are cheap reads, so two concurrent `doctor` runs harmlessly enqueue two
   jobs), and no retry (a transient probe failure dead-letters immediately to a clean `error`
   verdict rather than re-opening TLS / re-probing the ACL and cycling the job queued→running→queued
   under the dispatcher's poll, which would make the bounded wait race against the retry).
2. Polls `get_by_dedup_key` every ~0.25s until the job reaches a terminal state or the dispatch
   budget elapses.
3. On **succeeded** → parses the inline `result_ref` JSON into `CheckResult`s (a malformed/empty
   `result_ref` → an `error` verdict, never a crash).
4. On **failed** → returns one `error` `CheckResult` per worker-vantage check, carrying the job's
   `error_category` (e.g. `configuration_error` for a bad inventory).
5. On **budget reached with the job still queued/running** → returns `WORKER_UNAVAILABLE`
   substitutions (`transport_failure`, the `/livez`/`/readyz` detail) — the exact signal ADR-0139
   reserved for "dispatch exists but the worker cannot pick the job up." Never a hang.

The dispatcher owns the full worker-vantage outcome (run or substitute), keeping
`DiagnosticsService.run()` a simple merge.

#### Timing budget

The worker polls the queue every 1s (`WorkerConfig.poll_interval`) and runs two bounded probes, so
the dispatch needs a window comfortably above ~2s to avoid a spurious `WORKER_UNAVAILABLE` on a
healthy worker. A naive "give the dispatcher whatever overall budget remains after the
server-vantage checks" is unsafe: three server checks can each burn up to the 10s per-check timeout
against a slow host and starve the dispatcher to near-zero.

The fix is to **partition** the overall deadline into two bounded phases that *sum to* it, rather
than a floor that adds on top of an already-consumed deadline (a floor would let total runtime
exceed `overall_timeout`, voiding the gate guarantee that `doctor` reports a clean `error` instead
of running long). Concretely the overall budget grows to accommodate both phases: the server-phase
budget keeps today's full server-vantage allowance (`_DEFAULT_OVERALL_TIMEOUT`, ~30s — so server
checks are **not** regressed to a tighter budget), and a worker-phase budget
(`WORKER_DISPATCH_BUDGET`, ~15s) is added on for the dispatch, making the new overall ~45s. The
server checks run under the server-phase budget; the dispatcher is then always given its full
worker-phase budget regardless of how long the server phase took, so a slow server check cannot
starve it. A `WORKER_UNAVAILABLE` verdict therefore means a genuine pickup failure within a
guaranteed-adequate window, and total `doctor` runtime stays bounded (~45s) — well within the MCP
transport's in-flight tolerance (uvicorn keepalive bounds idle gaps between requests, not a single
in-flight call, per ADR-0138).

**Worker contention is a real cause of a genuine pickup failure.** `dequeue` claims the oldest
eligible job by `created_at`, and a typical deployment runs one worker. If `doctor` runs while a
minutes-long `provision`/`build` job is in flight, the diagnostics job waits behind it and the
worker phase elapses → `WORKER_UNAVAILABLE`. This is honest (the worker genuinely could not pick
the job up in time) but is *not* a worker outage, so the substituted detail is worded to cover both:
"worker did not pick up the diagnostic job in time — check that the worker is up (`/livez`,
`/readyz`) and not saturated." A priority lane for diagnostics jobs is out of scope (the queue is
FIFO by `created_at` with no priority column); the behavior is documented rather than engineered
around.

### 5. Service + factory wiring

`DiagnosticsService` gains an optional `worker_dispatcher: WorkerCheckDispatcher | None` and a list
of worker-vantage check specs (the `(check_id, provider)` pairs to dispatch). When a dispatcher is
present it is called with the remaining overall budget and its results are merged into the report;
the legacy `unavailable_worker_checks` + `substitution_reason` path is **only** used when
`worker_dispatcher is None`.

`default_service_factory` constructs the production `JobWorkerCheckDispatcher` (pool only) and
passes it when `is_remote_libvirt_configured()`. The factory therefore needs the pool, which
`ops.diagnostics.register` already has — so the factory closure captures it. When remote-libvirt is
not configured, the factory passes `worker_dispatcher=None` and the service keeps today's
`FEATURE_NOT_ENABLED` substitution (there are no worker-vantage checks to dispatch anyway). No
object store is involved in the diagnostics path.

### Substitution semantics (acceptance criterion 2)

| Deployment state | worker-vantage result |
|---|---|
| No dispatch wired (`worker_dispatcher is None`: remote-libvirt not configured) | `FEATURE_NOT_ENABLED` → `not_implemented` (unchanged) |
| Dispatch wired, worker runs the job | real `pass`/`fail`/`error` from the probes |
| Dispatch wired, job dead-letters (bad config / malformed result) | `error` with the job's `error_category` |
| Dispatch wired, worker never picks the job up within the dispatch budget | `WORKER_UNAVAILABLE` → `transport_failure`, "check /livez/readyz" |

This honors the AC's intent — no hang, no fabricated `fail`, an honest three-state `error` when the
check cannot run — and uses ADR-0139's `WORKER_UNAVAILABLE` for the genuine worker-down case (the
exact condition that ADR built the signal for). `not_implemented` is retained verbatim for
deployments that wire no dispatch.

## Testing

- **Probe units** — each probe maps its observable conditions to the right outcome with a fake
  socket/TLS connector: TLS handshake-ok → valid, `SSLCertVerificationError` → invalid,
  refused/timeout/other-`SSLError` → unreachable; ACL connect-ok/refused-fast → admit, timeout →
  blocked, other → indeterminate; `gdb_addr is None`.
- **Handler unit** — builds both checks, runs them, serializes + round-trips the inline JSON; a
  config-resolution failure propagates (job dead-letters); the serialized result contains no secret
  material (asserted against a registry seeded with a sentinel secret).
- **Dispatcher unit** — fake pool + a seeded job row: succeeded (inline result) → real results;
  failed → `error` with category; malformed `result_ref` → `error`; budget-with-no-pickup →
  `WORKER_UNAVAILABLE`; budget floor is honored when the passed-in remaining budget is near zero. A
  DB-backed test exercises enqueue→poll→complete once through the real queue.
- **Service unit** — with a fake dispatcher returning a fixed list, the report merges
  server-vantage + worker-vantage results; with `worker_dispatcher=None`, the legacy substitution
  is unchanged.
- **Default-factory unit** — remote-libvirt configured → a dispatcher-backed service (worker-vantage
  checks run/substitute via the dispatcher, not the static `FEATURE_NOT_ENABLED` path); not
  configured → `None` dispatcher → substitution retained.
- **Migration** — `test_migrate.py` already asserts the `jobs_kind_check`↔`JobKind` tie; the new
  value flows through it.
- **No live-hardware test.** Like reachability and base-image-staging, the live worker→host probe
  is hardware-gated; a runbook/OPERATOR-TODO records the live verification.

## Out of scope

- The `guest_egress` probe (separate opt-in, ADR-0091 §3) is untouched.
- Single-flighting / dedup of concurrent diagnostics jobs (cheap reads; not needed).
- Threading an allocation→resource→instance identity for multi-instance remote-libvirt (the whole
  diagnostics path already assumes the single declared instance, per `_resolve_instance`).
