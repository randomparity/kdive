# Spec — Surface `ssh_reachable` as a runtime probe (#972)

- **Issue:** #972 (follow-up split out of #956, deferred by ADR-0294 §5)
- **ADR:** [0298](../../adr/0298-ssh-reachable-runtime-probe.md)
- **Date:** 2026-07-02

## Problem

An agent that has a `ready` System and wants to run commands in the guest today
learns whether the guest sshd is reachable only by *attempting* `authorize_ssh_key`
(an OPERATOR mutation) and reading its `transport_failure`, or by waiting out a banner
timeout on its own SSH client. #956's review asked for a health signal a caller can read
*before* attempting SSH: "is this System's guest sshd answering right now?"

ADR-0294 fixed the underlying reachability (EL9 CPU model) and proved it per family, but
deferred the signal itself to this issue with the design fork stated: a **runtime probe**
on `systems.*` versus a **static image-capability signal** on `images.describe`.

## Decision (fork resolved)

Implement the **runtime probe**. It is the only layer that answers the per-System
liveness question the issue asks — a static image signal is an *image* fact and cannot
report whether a particular booted guest is answering now. The static `ssh_reachable`
`PlannedSignal` slot (`images/capability_signals.py`) is dropped, since the fork is now
settled against it.

## The `worker_loopback` constraint drives the shape

`systems.ssh_info` returns an endpoint scoped `worker_loopback`: the SSH forward is a
`127.0.0.1:<port>` mapping QEMU sets up **on the worker host**. The `systems.*` read
handlers run in the **server** process, which is thin and must not block or do guest I/O.
A synchronous server-side probe would be reachable only when server and worker share a
loopback (colocated dev) and silently wrong in a split deployment.

Therefore the probe runs **as a worker job** — the same execution vantage
`authorize_ssh_key` already uses to reach the guest — surfaced through the existing
`jobs.*` poll contract:

```
systems.check_ssh_reachable(system_id)  -> {job_id, status: running}   # VIEWER, enqueue
jobs.wait(job_id)                       -> status: succeeded,
                                           refs.result: <compact JSON verdict>
```

## Contract

### `systems.check_ssh_reachable(system_id)` (new tool, VIEWER)

Server handler mirrors `ssh_info`/`authorize_ssh_key` pre-checks, then enqueues:

1. Invalid UUID → `configuration_error` (`invalid_uuid`).
2. System absent or not in caller's projects → not-found-shaped error (no existence leak).
3. Caller lacks `viewer` on the owning project → `authorization_denied`.
4. System not `ready` → `readiness_failure` (SSH is a ready-only property).
5. Provider exposes no recorded loopback forward (`recorded_ssh_endpoint is None`) →
   `configuration_error` `reason="ssh_not_provisioned"` (identical to `ssh_info`; the
   probe *cannot run*, distinct from "ran and found it unreachable").
6. Otherwise enqueue `JobKind.CHECK_SSH_REACHABLE` and return the job handle.

**RBAC = VIEWER.** The probe is non-destructive observability that pairs with `ssh_info`
(also VIEWER): it opens one bounded TCP connection and reads the server banner, writing
nothing to the guest or the platform. It is *not* gated OPERATOR like `authorize_ssh_key`,
which mutates `authorized_keys`. (See ADR-0298 "Considered & rejected" for the
OPERATOR-vs-VIEWER trade.)

**Freshness (dedup).** A liveness probe is a point-in-time measurement, so each call mints
a **distinct** job: the `dedup_key` carries a fresh nonce
(`{system_id}:check_ssh_reachable:{uuid4}`). A static dedup_key would pin every future
probe to the first (succeeded, permanent-UNIQUE) job and report a stale verdict forever.

The cost of fresh-nonce is that probe jobs do **not** coalesce: this is the first
VIEWER-gated tool that enqueues durable work, so a VIEWER that loops the tool enqueues one
bounded probe job per call into the shared worker queue (which also serves
provision/build). That surface is accepted deliberately, not claimed away: each job is
bounded (short deadline + capped retry, below), the load is observable via `jobs.list`
depth, and a per-principal rate limit is the named follow-up if probe queue-pressure is
ever observed. In-flight coalescing is deliberately not built — freshness is the
requirement and coalescing machinery would be speculative for an authenticated,
project-scoped role. See ADR-0298 for the honest treatment of this trade.

### Worker handler (`JobKind.CHECK_SSH_REACHABLE`)

1. Load payload, **re-load the System**. If it is no longer `ready` (torn down / reprovisioning /
   failed between enqueue and run) → `CONFIGURATION_ERROR` `reason="system_not_ready"`. This
   is not just defensive: the recorded loopback port can be reallocated to a *different*
   System's forward after teardown, so probing a stale endpoint could return `reachable=true`
   and misattribute another guest's liveness. Re-checking `ready` closes that window.
2. Resolve the System's provider binding, read `recorded_ssh_endpoint`. `None` →
   `CONFIGURATION_ERROR` `reason="ssh_not_provisioned"` (the server tool already rejected —
   a race where the forward vanished between enqueue and run).
3. Probe `(host, port)` with a **bounded, retried** connect. The readiness race is real:
   local-libvirt declares a System `ready` ~46 ms before the guest sshd binds the forwarded
   port (`ssh_connect_retry.py`, ADR-0289), so a single connect fired right after `ready`
   gets an immediate RST and would report a false `reachable=false` — while
   `authorize_ssh_key`, the op this signal gates, retries connection-level failures for 90 s
   and would succeed. To avoid being *more pessimistic than the operation it gates*, the
   probe retries **connection-level** failures (refused / reset) with short backoff up to a
   total deadline of **`_PROBE_DEADLINE_S = 15.0`** (per-attempt connect timeout ~5 s), well
   under `jobs.wait`'s 30 s default so one poll observes a terminal result. It returns
   `reachable=true` as soon as a banner arrives. It **sends nothing** — sshd banners first;
   no handshake, no auth, no client banner. A far shorter window than `authorize`'s 90 s
   keeps the probe a *quick* check while still tolerating the bind race.
4. Read up to one banner line (≤255 bytes, RFC 4253) and classify into a fixed vocabulary
   (never echo raw guest bytes — the banner is external output):
   - banner received and begins `SSH-` → `reachable=true`, detail `"reachable"`
   - no connection accepted before the deadline (refused/reset throughout, or connect
     timeout) → `reachable=false`, detail `"unreachable"`
   - connected but no/short/non-`SSH-` banner before the deadline → `reachable=false`,
     detail `"no SSH banner"`
5. Return the verdict as a compact JSON string in `result_ref` (the ADR-0164 inline-verdict
   pattern; `result_ref` is already polymorphic across handlers):
   `{"reachable": bool, "checked_at": "<ISO-8601 UTC>", "endpoint": {"host","port"},
   "detail": "<one of the above>"}`. `checked_at` is stamped from an **injectable clock**
   (the `tests/clock.py` `FrozenClock` seam standardized after #931) so the serialized
   verdict is deterministic under test.

The job **succeeds whenever the probe ran** — `reachable=false` is a successful
measurement, not a job failure. Only an inability to *run* the probe (System no longer
ready, binding gone, no forward) dead-letters the job with an `error_category`. This keeps
queue-depth/failed metrics honest and lets the caller distinguish "definitively
unreachable" from "couldn't check".

The probe function is an injected seam (like `ssh_authorize`'s `ssh_exec`) so tests drive
the reachable / unreachable / no-banner / retry-then-succeed paths without a live guest.

## Out of scope / non-goals

- No static image-capability signal (the fork's rejected arm).
- No host-key verification or handshake completion — reachability, not authenticity.
- No change to `authorize_ssh_key`, `ssh_info` behavior beyond adding
  `systems.check_ssh_reachable` to `ssh_info`'s `suggested_next_actions`.
- No new `ErrorCategory` — reuse `configuration_error` (can't run) and the probe's own
  boolean (the answer).

## Acceptance criteria

- `systems.check_ssh_reachable` enqueues a job and returns `{job_id, status: running}` for
  a ready System with a recorded forward; rejects invalid-uuid / not-found / non-viewer /
  not-ready / no-forward with the categories above, each pre-enqueue.
- Two successive calls return **different** `job_id`s (freshness / no stale pinning).
- The worker handler returns `reachable=true` for a banner-answering endpoint (including a
  refused-then-answers endpoint within the retry window, proving the readiness-race
  tolerance) and `reachable=false` with `detail="unreachable"` when nothing answers before
  the deadline and `detail="no SSH banner"` when a connection is accepted but no `SSH-`
  banner arrives — never echoing raw banner bytes.
- A System no longer `ready` at handler time dead-letters with `configuration_error`
  `reason="system_not_ready"`; a `None` recorded endpoint dead-letters with
  `configuration_error` `reason="ssh_not_provisioned"`.
- `checked_at` is deterministic under an injected clock, so the serialized-verdict test
  asserts exact output.
- The verdict round-trips through `jobs.wait` as `refs.result` (compact JSON).
- Migration 0057 widens `jobs_kind_check` to admit `check_ssh_reachable`; the SQL↔enum tie
  and per-migration tests pass.
- `docs/guide/toolsets/systems.md` names the new tool (the #940 completeness guard);
  the wrapper docstring + `Field` text carry **no** `ADR-NNNN` reference (#880 guard).
- `full just ci` green.
