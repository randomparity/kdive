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
   `provider`; the handler re-resolves `remote_config_from_inventory()` at probe time on the worker,
   so no host identity or secret material rides on the queue.

2. **The worker handler runs the two real checks and returns their results as an object-store
   blob.** It builds `ProviderTlsCheck` + `GdbstubAclCheck` with production probes, runs each
   through `run_check` (per-check timeout → an unreachable host is an `error`, never a hang),
   serializes the `CheckResult`s to a small JSON blob, and returns its key as `result_ref` — the
   queue's existing result channel. The blob is operator-config-derived only and passes the
   redactor before storage as a defense-in-depth backstop.

3. **The probes.** `provider_tls` opens the `qemu+tls://` connection over the same path reachability
   uses: a clean open validates the chain → `pass`; a cert-verification failure → `fail` (reissue /
   `KDIVE_PROVIDER_CA`); a transport failure → `error`. `gdbstub_acl` is a *policy* check with no
   live listener: the worker attempts a TCP connect to `gdb_addr:<lowest port in range>` — success
   or a fast `ECONNREFUSED` means the ACL admits the range (`pass`); a connect timeout means the
   firewall drops it (`fail`); any other error is indeterminate (`error`). An unset `gdb_addr` is an
   `error` (cannot probe an unset address), not a guess.

4. **The server dispatches and bounded-waits behind a `WorkerCheckDispatcher` port.** The
   production dispatcher enqueues the job with a per-call-unique `dedup_key` (no single-flight —
   these are cheap reads, unlike the guest-provisioning egress probe), polls the job row until
   terminal or the remaining overall deadline elapses, then maps: succeeded → the real results;
   dead-lettered → `error` carrying the job's `error_category`; deadline-with-no-pickup →
   `WORKER_UNAVAILABLE` (`transport_failure`, the `/livez`/`/readyz` detail). The dispatcher owns
   the entire worker-vantage outcome, keeping `DiagnosticsService.run()` a simple merge.

5. **`default_service_factory` wires the dispatcher when remote-libvirt is configured and a store is
   available.** When the object store is unconfigured the factory passes `worker_dispatcher=None`
   and the service falls back to today's `FEATURE_NOT_ENABLED` substitution rather than crashing —
   so the honest non-hang signal is preserved across every degraded state.

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
  provider seam, the agent-facing tool surface, or the `ops.diagnostics` request/response shape.
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
- **A live-port `gdbstub_acl` check against a running debug target.** Rejected by ADR-0091 §2: the
  gdbstub port is per-domain and a cold preflight has no concrete port; the range/policy check
  catches the closed-ACL fault without provisioning a guest.
