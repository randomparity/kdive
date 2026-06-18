# ADR 0163 — Diagnostics worker-vantage dispatch (`provider_tls`, `gdbstub_acl`)

- **Status:** Proposed
- **Date:** 2026-06-17
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):**
  [ADR-0091](0091-doctor-diagnostics-model.md) (the `Check`/three-state/vantage model and the
  single-coherent-verdict contract; the worker-vantage checks run as jobs),
  [ADR-0125](0125-diagnostics-host-reachability.md) (the server-vantage `qemu+tls://` reachability
  probe whose connection lifecycle the TLS probe reuses),
  [ADR-0139](0139-diagnostics-worker-vantage-substitution-honesty.md) (the
  `FEATURE_NOT_ENABLED` vs `WORKER_UNAVAILABLE` substitution honesty — this ADR is the capability
  follow-up it explicitly deferred),
  [ADR-0083](0083-remote-connect-debug-plane.md) (the remote debug client runs worker-side,
  which fixes the vantage),
  [ADR-0079](0079-remote-live-debug-transport.md) (`gdb_addr` is the ACL'd security boundary the
  ACL check probes),
  [ADR-0018](0018-job-queue-worker-execution.md) (the durable job queue this dispatches through).
- **Spec:** [`../superpowers/specs/2026-06-17-diagnostics-worker-vantage-dispatch.md`](../superpowers/specs/2026-06-17-diagnostics-worker-vantage-dispatch.md)

## Context

`ops.diagnostics` (ADR-0091) assembles two worker-vantage checks, `provider_tls` and
`gdbstub_acl`, but wires no worker-job dispatch for them. The default factory builds the service
with `worker_available=False`, and ADR-0139 made the resulting substitution name its cause —
`FEATURE_NOT_ENABLED` → `not_implemented` — while deferring the actual capability. The two checks
therefore never run, which is a coverage gap for the remote-libvirt debug workflow whose
gdbstub-reachability they exist to confirm (issue #514).

The vantage is load-bearing, not cosmetic. ADR-0091 §2 says these checks must run "from the host
the real debug session connects from"; ADR-0083 puts that host on the worker. The worker's network
path to `gdb_addr` may differ from the server's, so probing from the server would validate the
wrong hop and false-green the M2 fault (a closed ACL). The checks must execute on the worker.

The job queue (ADR-0018) is the only server→worker handoff in the codebase. `ops.diagnostics`
returns one coherent verdict (ADR-0091 §1), and that ADR already pre-decided the failure mode: a
worker that cannot pick the job up surfaces as an `error` pointing at the health endpoints, "not a
hang."

## Decision

1. **A new durable job kind, `diagnostics_worker_check`, carries the worker-vantage checks to the
   worker.** It is added to `JobKind` and admitted by an additive migration that drops and recreates
   `jobs_kind_check` (mirroring the `image_build` precedent, ADR-0092). Its payload carries only the
   `provider` (the concrete `remote-libvirt` id — these checks are remote-libvirt-specific and
   gated on `is_remote_libvirt_configured()`, mirroring today's assembly, not the tool's nullable
   `provider` target); the handler re-resolves `remote_config_from_inventory()` at probe time on the
   worker, so no host identity or secret material rides on the queue.

2. **The worker handler runs the two real checks and returns their results inline.** It builds
   `ProviderTlsCheck` + `GdbstubAclCheck` with production probes, runs each through `run_check`
   (per-check timeout → an unreachable host is an `error`, never a hang), serializes the
   `CheckResult`s to a compact JSON string, and returns it inline as `result_ref`. The verdict is
   small, operator-config-derived (no tenant data, no secret material), and read only by the
   dispatcher, so it rides inline rather than as an object-store blob — which avoids an unreaped
   per-run blob (every `doctor` run uses a unique `dedup_key`) and keeps the object store out of the
   diagnostics path. The probes never place secret material in `detail`/`fix` (asserted by test);
   that is the control, not an op-scoped redactor over a result that holds no secrets.

3. **The probes.** `provider_tls` does a direct TLS handshake (Python `ssl`) to the libvirt TLS
   endpoint — host and port parsed from `config.uri` (default `16514` only when absent) so it
   targets the same endpoint the worker uses — with the materialized client cert/key and the
   configured CA. It is *not* a libvirt open, because `remote_connection` wraps a failed qemu+tls
   open opaquely as `TRANSPORT_FAILURE`, which cannot tell a bad cert from a down host (the
   distinction this check exists to make). Handshake OK → `pass`; `ssl.SSLCertVerificationError` →
   `fail` (reissue / set the provider CA); refused / timeout / other `SSLError` → `error` (the safe
   direction — an ambiguous handshake is "could not validate," never a fabricated `fail`).
   Classifying on the typed `ssl` exception keeps the verdict stable across libvirt versions. The
   check is scoped to chain validity, not libvirt's `tls_allowed_dn_list` authz (which surfaces via
   reachability/provision). `gdbstub_acl` is a *policy* check with no live listener: the
   worker attempts a TCP connect to `gdb_addr:<lowest port in range>` — connect or fast
   `ECONNREFUSED` → `pass` (the SYN reached the host TCP stack, excluding the M2 `DROP`/blackhole
   fault); connect timeout → `fail` (the firewall drops it); any other error → `error`. An unset
   `gdb_addr` is an `error`, not a guess.

4. **The server dispatches and bounded-waits behind a `WorkerCheckDispatcher` port.** The
   production dispatcher enqueues the job with a per-call-unique `dedup_key` and `max_attempts=1`
   (no single-flight — these are cheap reads, unlike the guest-provisioning egress probe; no retry —
   a transient failure dead-letters to a clean `error` instead of cycling the job under the
   dispatcher's poll), polls the job row until terminal or the **reserved dispatch budget** elapses,
   then maps: succeeded → the real results (malformed inline result → `error`); dead-lettered →
   `error` carrying the job's `error_category`; budget-with-no-pickup → `WORKER_UNAVAILABLE`
   (`transport_failure`, a "did not pick up in time — check the worker is up and not saturated"
   detail). The overall deadline is **partitioned** into a bounded server phase and a bounded worker
   phase that sum to it (not a floor added on top, which could push runtime past the deadline and
   void the gate guarantee), so the dispatcher always gets its full worker-phase budget regardless
   of how long the server checks ran — a slow server check cannot manufacture a spurious
   `WORKER_UNAVAILABLE`. A busy single worker (the diagnostics job waiting behind a long
   provision/build, since the queue is FIFO by `created_at`) is a genuine in-time pickup failure and
   is covered by that same honest detail. The dispatcher owns the entire worker-vantage outcome,
   keeping `DiagnosticsService.run()` a simple merge.

5. **`default_service_factory` wires the dispatcher when remote-libvirt is configured.** It captures
   the pool (no object store). When remote-libvirt is not configured the factory passes
   `worker_dispatcher=None` and the service keeps today's `FEATURE_NOT_ENABLED` substitution — so
   the honest non-hang signal is preserved across every degraded state.

## Consequences

- `provider_tls` and `gdbstub_acl` produce real three-state verdicts in a remote-libvirt
  deployment with a healthy worker, closing the ADR-0139 deferral and the #514 coverage gap.
- The worker-down case now reports `WORKER_UNAVAILABLE` (`transport_failure`, "check
  /livez/readyz"), the signal ADR-0139 built for exactly this condition. `not_implemented` is
  retained verbatim for deployments that wire no dispatch (no remote-libvirt, or no object store),
  so acceptance criterion 2 (an honest substitution, never a hang or fabricated `fail`) holds in
  every state — see the spec's substitution-semantics table.
- New surface: a `JobKind` value + migration `0040`, a payload model, a worker handler
  (`jobs/handlers/diagnostics.py`) registered in `_HANDLER_REGISTRARS`, two probe adapters, a
  dispatcher module, and an optional `DiagnosticsService` constructor argument. No change to the
  provider seam, the agent-facing tool surface, the object store, or the `ops.diagnostics`
  request/response shape.
- `gdbstub_acl` catches a `DROP`/blackholed range (the observed M2 fault) and the no-rule case, but
  a fast `ECONNREFUSED` cannot distinguish "no listener" from an iptables `-j REJECT` rule, so a
  REJECT-style block reads as `pass`. This is an accepted limitation; the `pass` detail is worded
  "host TCP stack reachable on the gdbstub range," not "the ACL fully admits it."
- The live worker→host probe is hardware-gated (like reachability/base-image-staging); CI verifies
  the three-state mapping against injected fakes, and an OPERATOR-TODO records the live run.

## Considered & rejected

- **Reclassify the checks as server-vantage and probe from the server** (like
  `remote_libvirt_reachability`). Rejected by ADR-0091 §2: the worker's path to `gdb_addr` is the
  one the real debug session uses; a server-side probe validates the wrong hop and would
  false-green a worker-only ACL block — the exact M2 fault this check exists for.
- **Return `{job_id, running}` and let the operator poll `jobs.*` for the worker-vantage slice.**
  Rejected: it breaks ADR-0091 §1's single-coherent-verdict contract and splits `doctor`'s output
  across two calls and a result-merge the operator must do by hand.
- **Keep `not_implemented` even when dispatch is wired and the worker is down.** Rejected: once the
  feature is enabled, "not enabled in this deployment" is the misdirection ADR-0139 set out to
  remove; the honest cause is a worker outage, which `WORKER_UNAVAILABLE` names and points at the
  health endpoints.
- **Single-flight the diagnostics job per provider** (as the egress probe is). Rejected as
  unneeded: these checks only read, so two concurrent `doctor` runs enqueuing two jobs costs
  nothing — the single-flight guard exists for the egress probe because it provisions a guest.
- **Return the worker result as an object-store blob** (the usual `result_ref` = object key
  convention). Rejected: with a per-call-unique `dedup_key` every `doctor` run would leave a blob
  and nothing reaps diagnostics blobs (unbounded orphan growth), and it would drag the object store
  into the server-side diagnostics path. The verdict is two small, non-secret `CheckResult`s read
  only by the dispatcher, so it rides inline in `result_ref` — a deliberate, scoped exception.
- **Classify `provider_tls` by libvirt's connection-open outcome / error message.** Rejected: a
  failed qemu+tls open is wrapped opaquely as `TRANSPORT_FAILURE`, so a bad cert and a down host are
  indistinguishable there, and error-string matching is version-fragile. A direct `ssl` handshake
  yields a typed `SSLCertVerificationError` that separates `fail` (bad cert) from `error` (host
  down) unambiguously.
- **A live-port `gdbstub_acl` check against a running debug target.** Rejected by ADR-0091 §2: the
  gdbstub port is per-domain and a cold preflight has no concrete port; the range/policy check
  catches the closed-ACL fault without provisioning a guest.
