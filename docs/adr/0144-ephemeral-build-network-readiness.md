# ADR 0144 — Ephemeral build-VM network-readiness gate + surface `git fetch`'s return code

- **Status:** Proposed
- **Date:** 2026-06-17
- **Deciders:** kdive maintainers

## Context

A git-lane build on an `ephemeral_libvirt` build host (#500, ADR-0100) fails its clone with a
misleading error. Two independent defects compound:

1. **Readiness gap: agent-ready ≠ network-ready.** `EphemeralBuildVm.session`
   (`lifecycle/build_vm.py`) waits only for the qemu-guest-agent
   (`wait_for_agent`, `lifecycle/readiness.py`). The agent is device-activated off the
   virtio-serial channel and connects ~boot+2s — *before* the guest finishes DHCP. The first
   command kdive runs over the agent is the git clone
   (`transport_seams._checkout` → `ShellBuildTransport.clone`), which needs egress to the git
   remote, so it fails on a guest whose NIC has no lease yet.

2. **`clone()` masks the real cause.** `ShellBuildTransport.clone()`
   (`shared/build_host/shell_transport.py`) runs `git init` → `git fetch --depth 1` →
   `git checkout FETCH_HEAD` but checks only the **checkout**'s return code. When the fetch
   fails (no network yet), `FETCH_HEAD` is never written and the failure surfaces as
   `git checkout FETCH_HEAD failed … pathspec 'FETCH_HEAD' did not match any file(s)`, hiding
   the actual fetch/network error.

Controlled experiment (real hardware, host ub24-big, build host `ub24-big-build`): the build VM
spins up correctly, but the clone fails immediately; booting the same image manually, the agent
answers at ~boot+2-5s and the NIC gets its DHCP lease *after*, and once the network is up the
exact `git fetch --depth 1 … v6.12` + `git checkout FETCH_HEAD` succeeds (rc 0). The only
difference between success and failure is whether the guest network was up when the clone ran.

Constraint from the field (operator note in #500): gating `qemu-guest-agent.service` on
`network-online.target` **in the build image** is not a fix — the agent is device-activated, so
the ordering makes it start/flap and `wait_for_agent` sees it connect then drop mid-build. The
fix must live in the build-VM readiness logic, not the guest image.

## Decision

Two changes, at the layers that own each failure.

**1. Gate the transport on in-guest network readiness (build-VM readiness, provider-side).**
After `wait_for_agent` confirms the agent channel is connected, `EphemeralBuildVm.session` polls
an **in-guest default-route probe** over the guest agent until a default route is present or a
bounded deadline elapses, **before** yielding the transport — so the clone (the first caller
operation) runs against a network-ready guest.

- The probe is one `/bin/sh -c 'cut -f2 /proc/net/route | grep -qx 00000000'` hop run through the
  existing `GuestExecBuildTransport.run` (allowlist `{'/bin/sh'}`, unchanged). A default route
  appears exactly when DHCP completes (the lease installs the IP *and* the default route
  together), so its presence is the precise "DHCP done" signal. Reading `/proc/net/route` is
  kernel truth and needs only `cut` + `grep` — no `iproute2` dependency on the build image.
- A probe returning a non-zero exit code means "not ready yet, keep polling." A raised
  `CategorizedError` (agent unreachable) **propagates** unchanged: `wait_for_agent` already
  confirmed the channel connected, so a drop during the probe is a genuine `transport_failure`,
  not a not-ready signal.
- A new `wait_for_network(probe, …)` poll loop lives in `lifecycle/readiness.py` next to
  `wait_for_agent`, taking the same injected `monotonic`/`sleep`. New `BuildVmTiming` fields
  `network_timeout_s` (default 120s) and `network_poll_s` (default 2s) make it injectable; on
  deadline it raises `PROVISIONING_FAILURE` ("guest network did not come up within Ns"),
  consistent with the agent-timeout failure on the same path.

**2. Surface `git fetch`'s return code in `clone()` (clone failure contract).**
`ShellBuildTransport.clone()` now checks the `git init` and `git fetch` return codes, not only
`checkout`:

- `git init` non-zero → `INFRASTRUCTURE_FAILURE` ("git init failed on remote" + redacted stderr)
  — a failed init is an environment/filesystem fault, not a bad ref.
- `git fetch` non-zero → `CONFIGURATION_ERROR` ("git fetch failed on remote" + redacted stderr).
- `git checkout FETCH_HEAD` non-zero → `CONFIGURATION_ERROR` (unchanged).

All three reuse the existing `redacted_tail(result.stderr, self._secret_registry)` so a remote
URL credential cannot leak into an error detail.

## Consequences

- The ephemeral-libvirt git build lane no longer fails its clone with a misleading
  FETCH_HEAD/pathspec error when the guest network is merely slow to come up: the session waits
  for the default route, then the clone runs against a network-ready guest.
- A genuinely unreachable remote or bad ref now surfaces with the fetch's own stderr and an
  accurate message ("git fetch failed on remote"), so triage points at the real cause instead of
  a downstream checkout error.
- The fix is in the build-VM readiness logic (provider), not the guest image — honoring the
  operator constraint that gating the agent service on `network-online.target` flaps the agent.
- Slight added latency on every ephemeral build: one extra in-guest probe round-trip after the
  agent connects (typically a single poll, since the route is usually already up by then),
  bounded by `network_timeout_s`.
- `SshBuildTransport` shares `clone()`, so it also gains the fetch-rc surfacing — strictly an
  improvement. The readiness gate is specific to the ephemeral build-VM session and does not
  touch the SSH lane (its host network is already up).
- No new MCP tool, schema field, DB column, migration, env-var, or auth-model change. The probe
  timing is a constructor default (injectable), mirroring `_AGENT_TIMEOUT_S`.

## Considered & rejected

- **Gate `qemu-guest-agent.service` on `network-online.target` in the build image.** Rejected,
  and explicitly called out by #500: the agent is device-activated off virtio-serial; ordering
  it after network makes it start/flap, so `wait_for_agent` sees it connect and then drop
  mid-build. The fix must be in the readiness logic, not the image.
- **Reachability probe to the git remote (TCP connect / `git ls-remote`).** Rejected as
  over-coupling: it requires parsing the remote URL, assumes the remote is reachable by the
  probe's method (an SSH or private remote is not HTTP-probeable), and conflates "network up"
  with "this specific remote up." A default route is the minimal precise differentiator the
  evidence points to.
- **DNS-resolution probe (`getent hosts <remote-host>`).** Rejected: it needs the remote host
  parsed out of the URL and assumes `resolv.conf` is the failure axis; DHCP installs the route
  and `resolv.conf` together, so the default-route check already covers the DNS case without URL
  parsing.
- **Retry the clone/fetch on transient failure with backoff (instead of a readiness gate).**
  Rejected as the primary mechanism: it couples readiness to the build operation, retries an
  operation with a 10-minute timeout (a slow first attempt can burn the budget before a retry),
  and muddies the failure contract (a bad ref would be retried pointlessly). A pre-clone
  readiness gate is cheaper and keeps `clone()` a pure operation. The fetch-rc surfacing (change
  2) still makes a genuine post-gate failure honest.
- **Categorize a `git fetch` failure as `INFRASTRUCTURE_FAILURE`/`TRANSPORT_FAILURE`
  (network-shaped).** Rejected: the readiness gate removes the network-not-ready case before the
  fetch runs, so a post-gate fetch failure is overwhelmingly a bad remote/ref — a
  non-retryable `CONFIGURATION_ERROR` is the honest category, with the stderr surfaced for the
  rare residual network case. `git fetch` returns 128 for both "couldn't resolve host" and
  "couldn't find ref," so the clone layer cannot reliably distinguish them; mapping to the
  dominant post-gate cause is the least-surprising choice.
