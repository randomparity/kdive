# ADR 0298 — Surface `ssh_reachable` as a worker-job runtime probe

- **Status:** Accepted
- **Date:** 2026-07-02
- **Deciders:** KDIVE maintainers

## Context

Issue #972 (split from #956, deferred by ADR-0294 §5) asks for an `ssh_reachable` signal
so an agent can tell, *before* attempting SSH, whether a `ready` System's guest sshd is
answering. ADR-0294 fixed the underlying reachability (the EL9 `qemu64`/x86-64-v2 panic)
and proved it per family, but left the *signal* deliberately unbuilt with the design fork
stated:

- a **runtime probe** on `systems.*` — a live TCP-connect + SSH-banner check returning a
  fresh per-call answer (what the #956 review text and this issue's title literally ask
  for); or
- a **static image-capability signal** on `images.describe` — the existing
  `PlannedSignal("ssh_reachable")` slot in `images/capability_signals.py` (ADR-0286),
  computed over build-recorded provenance.

They answer different questions. Only the runtime probe answers "is *this* booted guest
answering *now*"; the static signal is an *image* fact ("this image is built to be
SSH-capable") and cannot speak to a particular System's liveness.

Two facts about the existing surface shape the design:

- **`ssh_info`'s endpoint is `worker_loopback`.** The SSH forward is a `127.0.0.1:<port>`
  mapping QEMU sets up on the **worker** host (ADR-0281). The `systems.*` read handlers run
  in the **server** process, which is thin and must not block or perform guest I/O. A
  synchronous server-side probe is correct only when server and worker share a loopback
  (colocated dev) and silently wrong in a split deployment.
- **The queue already reaches the guest.** `authorize_ssh_key` runs on the worker and
  root-SSHes the guest over that same loopback forward (ADR-0271/0218). The worker is the
  correct — and already-proven — vantage for touching the guest.

## Decision

Implement the runtime probe **as a worker job**, surfaced through the existing `jobs.*`
poll contract. Drop the static `ssh_reachable` `PlannedSignal` — the fork is now settled
against it.

1. **New tool `systems.check_ssh_reachable(system_id)` (VIEWER).** It performs the same
   pre-checks as `ssh_info` (valid uuid; project-scoped not-found; `viewer`; `ready`;
   `recorded_ssh_endpoint is not None` → else `configuration_error`
   `reason="ssh_not_provisioned"`), then enqueues one `JobKind.CHECK_SSH_REACHABLE` job and
   returns its `{job_id, status: running}` handle.

2. **RBAC = VIEWER.** The probe is non-destructive observability that pairs with `ssh_info`
   (VIEWER): it opens one bounded TCP connection and reads the server's banner, writing
   nothing to the guest or the platform. Gating it OPERATOR (as `authorize_ssh_key` is,
   because that *mutates* `authorized_keys`) would let a caller read the endpoint but not
   test it.

3. **Fresh per call.** A liveness probe is a point-in-time measurement, so the `dedup_key`
   carries a fresh nonce (`{system_id}:check_ssh_reachable:{uuid4}`). Each call mints a
   distinct job. A static dedup_key would pin every future probe to the first — succeeded,
   permanent-`UNIQUE` — job and report a stale verdict forever.

4. **Worker handler probes and returns an inline verdict.** It resolves the binding, reads
   `recorded_ssh_endpoint`, opens a TCP connection to `(host, port)` under a bounded
   deadline, reads at most one banner line (≤255 bytes, RFC 4253), and classifies without
   sending anything (sshd banners first; no handshake, no auth). It returns a compact JSON
   verdict `{"reachable", "checked_at", "endpoint", "detail"}` as `result_ref` — the
   ADR-0164 inline-verdict pattern; `result_ref` is already polymorphic (object-id /
   artifact-key / inline JSON) and nothing auto-resolves it as an artifact. `jobs.wait`
   surfaces it as `refs.result`.

5. **Reachable-false is a job *success*.** The job succeeds whenever the probe *ran*;
   `reachable=false` (refused / timed out / no banner) is a successful measurement.
   Only an inability to run — a `None` endpoint at handler time (a forward that vanished
   between enqueue and run) — dead-letters the job with `configuration_error`. This keeps
   the caller able to distinguish "definitively unreachable" from "couldn't check", and
   keeps `jobs.list` failed-depth metrics honest.

6. **Redact the banner.** The banner is guest-originated external output, so the handler
   classifies it into a fixed detail vocabulary
   (`reachable` / `connection refused` / `timed out` / `no SSH banner`) and never echoes the
   raw `SSH-2.0-OpenSSH_…` bytes into the persisted verdict.

Migration 0056 widens the `jobs_kind_check` constraint to admit `check_ssh_reachable`
(forward-only, drop-and-recreate to keep the constraint name stable, per the 0052/0055
pattern). No other schema, RBAC role, `ErrorCategory`, or config change.

## Consequences

- An agent can call `systems.check_ssh_reachable` → `jobs.wait` and read a fresh boolean
  liveness verdict before committing to `authorize_ssh_key` + SSH, over a transport that is
  correct in both colocated and split server/worker deployments.
- **First VIEWER-gated tool that enqueues a durable job.** Every prior job-enqueuing tool
  is OPERATOR+ (provision, authorize, power) or platform (diagnostics). The worker-load
  surface a VIEWER gains is bounded and self-limiting: one job per call, a single
  short-deadline connect, no retry. Accepted deliberately; noted here so a future reviewer
  sees it was a choice, not an oversight.
- **Non-coalescing.** Fresh-nonce dedup means two truly-concurrent identical calls run two
  probes. Negligible for a bounded connect; the alternative (a stale static dedup) is worse.
- **`result_ref` gains another inline-verdict user.** It is now object-id, artifact-key, or
  inline JSON depending on job kind. This is pre-existing polymorphism (ADR-0164), not new,
  but it does mean `refs.result` is not always a resolvable artifact reference — a reader
  keys interpretation off the job `kind` (already surfaced in `data.kind`).
- **The static `ssh_reachable` signal is gone, not deferred.** `images.describe` will not
  grow an `ssh_reachable` capability block. If an image-level "built SSH-capable" fact is
  ever wanted, it is a new, differently-named signal — this ADR closes the #956 fork.
- **Providers without a loopback forward** (remote/fault-inject paths where
  `recorded_ssh_endpoint` is `None`) reject at the server tool with `ssh_not_provisioned`,
  the same as `ssh_info`/`authorize_ssh_key`. The probe is a local-libvirt-shaped capability
  by the same reasoning as ADR-0271.

## Considered & rejected

- **Static image-capability signal (the fork's other arm).** Rejected as the answer to
  #972: it cannot report per-System liveness, which is the question asked. Dropping the
  `PlannedSignal` rather than leaving it "planned" keeps `capability_signals.py` honest —
  the decision is made, not pending.

- **Synchronous server-side probe returning a boolean field on `systems.get`/`ssh_info`.**
  Simplest UX (no polling), but correct only when server and worker share a loopback. In
  the multi-user HTTP architecture they are separate processes/hosts, so the probe would
  connect to the *server's* 127.0.0.1 and silently misreport. Rejected as a correctness
  trap; the worker job is the honest vantage.

- **RBAC = OPERATOR.** Consistent with `authorize_ssh_key` and with "no prior VIEWER tool
  enqueues a job." Rejected: the probe mutates nothing and is observability that naturally
  pairs with the VIEWER `ssh_info`; requiring OPERATOR to *test* an endpoint a VIEWER can
  *read* is incoherent. The enqueue-surface concern is mitigated by the bounded, no-retry
  probe.

- **Reachable-false as a job failure (`transport_failure`).** Mirrors
  `authorize_ssh_key`'s failure path and needs no result surfacing. Rejected: it conflates
  a successful measurement ("ran, answer is no") with an operational failure ("couldn't
  run"), pollutes failed-job metrics, and loses the very distinction the caller needs.

- **Static `dedup_key` (`{system_id}:check_ssh_reachable`).** Idempotent like
  `authorize_ssh_key`. Rejected: `enqueue` returns the same job forever on a `dedup_key`
  conflict, so the second probe would read the first probe's stale verdict — fatal for a
  liveness signal.

- **Complete the SSH handshake / verify the host key.** Rejected as scope creep: the
  question is reachability, not authenticity. Reading the server banner is sufficient and
  minimal (no auth, no client bytes).

- **A dedicated `systems.reachability_result` reader instead of `jobs.wait`.** Rejected:
  the durable-job poll contract (`jobs.wait` → `refs.result`) already surfaces the verdict;
  a second reader duplicates it. The inline-verdict-in-`result_ref` precedent (ADR-0164)
  already exists.
