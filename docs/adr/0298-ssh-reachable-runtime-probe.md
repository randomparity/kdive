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

4. **Worker handler re-checks state, probes with a bounded retry, returns an inline
   verdict.** It re-loads the System and dead-letters `configuration_error`
   `reason="system_not_ready"` if it is no longer `ready` — the recorded loopback port can be
   reused by another System's forward after teardown, so probing a stale endpoint could
   misattribute another guest's liveness. It then resolves the binding, reads
   `recorded_ssh_endpoint`, and opens a TCP connection to `(host, port)`, **retrying
   connection-level failures** (refused/reset) with short backoff up to a 15 s deadline
   (`_PROBE_DEADLINE_S`, per-attempt connect ~5 s), reads at most one banner line (≤255
   bytes, RFC 4253), and classifies without sending anything (sshd banners first; no
   handshake, no auth). The bounded retry exists because local-libvirt declares `ready` ~46
   ms before the guest sshd binds (ADR-0289): a single no-retry connect fired right after
   `ready` would report a false `reachable=false` while `authorize_ssh_key` — the op this
   signal gates, which retries for 90 s — would succeed. A 15 s window (far shorter than
   authorize's 90 s, well under `jobs.wait`'s 30 s default) tolerates the race while keeping
   the probe quick. `checked_at` is stamped from an injectable clock (the #931 `FrozenClock`
   seam) for deterministic test output. It returns a compact JSON verdict `{"reachable",
   "checked_at", "endpoint", "detail"}` as `result_ref` — the ADR-0164 inline-verdict
   pattern; `result_ref` is already polymorphic (object-id / artifact-key / inline JSON) and
   nothing auto-resolves it as an artifact. `jobs.wait` surfaces it as `refs.result`.

5. **Reachable-false is a job *success*.** The job succeeds whenever the probe *ran*;
   `reachable=false` (nothing answered before the deadline, or no `SSH-` banner) is a
   successful measurement. Only an inability to run — the System no longer `ready`, or a
   `None` endpoint at handler time — dead-letters the job with `configuration_error`. This
   keeps the caller able to distinguish "definitively unreachable" from "couldn't check",
   and keeps `jobs.list` failed-depth metrics honest.

6. **Redact the banner.** The banner is guest-originated external output, so the handler
   classifies it into a fixed detail vocabulary
   (`reachable` / `connection refused` / `timed out` / `no SSH banner`) and never echoes the
   raw `SSH-2.0-OpenSSH_…` bytes into the persisted verdict.

Migration 0057 widens the `jobs_kind_check` constraint to admit `check_ssh_reachable`
(forward-only, drop-and-recreate to keep the constraint name stable, per the 0052/0055
pattern). No other schema, RBAC role, `ErrorCategory`, or config change.

## Consequences

- An agent can call `systems.check_ssh_reachable` → `jobs.wait` and read a fresh boolean
  liveness verdict before committing to `authorize_ssh_key` + SSH, over a transport that is
  correct in both colocated and split server/worker deployments.
- **First VIEWER-gated tool that enqueues a durable job, and it does not coalesce.** Every
  prior job-enqueuing tool is OPERATOR+ (provision, authorize, power) or platform
  (diagnostics). Fresh-nonce dedup (needed so a liveness verdict is never pinned to a stale
  prior job) means a looping VIEWER enqueues one probe job per call into the shared worker
  queue — it is *not* self-limiting across calls, so this is a real, if low-severity,
  queue-pressure surface for an authenticated, project-scoped role. It is accepted
  deliberately, with three mitigations rather than a claimed-away bound: each job is bounded
  (15 s deadline, capped retry), the load is observable via `jobs.list` depth, and a
  per-principal rate limit is the named follow-up if probe queue-pressure is observed.
  In-flight coalescing was considered and rejected as speculative machinery (see below).
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

- **In-flight coalescing dedup (reuse a queued/running probe, mint fresh only when the prior
  is terminal).** Would blunt the fresh-nonce queue-pressure surface while preserving
  freshness. Rejected for now as speculative machinery: the current `enqueue` primitive
  returns the same job on a key conflict and only resets `failed` jobs, so coalescing needs a
  new "latest non-terminal probe for this System" lookup path built for a DoS vector an
  authenticated project-scoped VIEWER could approximate through other bounded reads. Freshness
  is the hard requirement; coalescing is a per-principal rate-limit follow-up if the observed
  `jobs.list` depth ever warrants it (YAGNI until then).

- **Single no-retry connect (a pure point-in-time snapshot).** Simplest and most literally
  "right now". Rejected: local-libvirt declares `ready` ~46 ms before sshd binds (ADR-0289),
  so a connect fired immediately after `ready` gets an RST and reports a false
  `reachable=false`, while `authorize_ssh_key` (the gated op, which retries 90 s) would
  succeed — the signal would be *more pessimistic than the operation it gates* at the exact
  moment an agent calls it. A bounded 15 s connection-level retry tolerates the race while
  staying far shorter than authorize and under `jobs.wait`'s window.

- **Complete the SSH handshake / verify the host key.** Rejected as scope creep: the
  question is reachability, not authenticity. Reading the server banner is sufficient and
  minimal (no auth, no client bytes).

- **A dedicated `systems.reachability_result` reader instead of `jobs.wait`.** Rejected:
  the durable-job poll contract (`jobs.wait` → `refs.result`) already surfaces the verdict;
  a second reader duplicates it. The inline-verdict-in-`result_ref` precedent (ADR-0164)
  already exists.
