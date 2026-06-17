# ADR 0155 — Build-VM egress preflight to the configured source (not just a default route)

- **Status:** Accepted
- **Date:** 2026-06-17
- **Deciders:** kdive maintainers

## Context

The ephemeral remote-libvirt build VM (#500, ADR-0100) gates its readiness on an **in-guest
default-route probe** before yielding the build transport (ADR-0144): once the guest has a
default route, the session yields and the caller runs the source clone
(`ShellBuildTransport.clone` → `git fetch`).

ADR-0144's premise was "a default route appears exactly when DHCP completes, so its presence is
the precise *network-is-up* signal, and *network-up* covers reachability of the source without
URL parsing." A live remote-build campaign on real hardware (host `ub24-big`) falsified that
premise: the host network was **verified valid** (FORWARD ACCEPT, default libvirt net in NAT
mode) and the build VM **had a default route**, yet the clone still failed — the guest could not
actually reach `github.com`. The build surfaced as a confusing clone/fetch error rather than a
clear "no egress to source" signal (campaign build failures `6b99aa8d`, `d39a408e`; #519).

A default route is necessary but **not sufficient** for egress to a specific remote: DNS may be
broken, the guest subnet→internet hop may be policy-dropped while the route still exists, or the
remote host/port may be unreachable from the guest's vantage even though the guest is otherwise
"on the network." The route probe cannot observe any of these — it confirms only that a lease
landed.

This ADR revisits the **"reachability probe to the git remote"** alternative that ADR-0144
considered and rejected. That rejection rested on three claims, each falsified or avoidable:

- *"Requires parsing the remote URL."* Avoided: we run `git ls-remote <remote>` in-guest and let
  **git** parse the remote and dial it — we never parse a host/port out of the URL ourselves.
- *"An SSH or private remote is not HTTP-probeable."* Avoided: `git ls-remote` speaks the
  remote's **own** protocol (https/ssh/git), so it probes exactly the transport the clone will
  use — not a substituted HTTP guess.
- *"Conflates network-up with this-remote-up."* That conflation **is the bug**: route-up was
  already verified yet egress to the source failed. #519 specifically wants "can this build VM
  reach *this source*," checked before the clone.

The default-route gate from ADR-0144 stays. This ADR **adds** a second, source-specific
precondition after it. ADR-0144 is not reverted; its route gate remains correct (a VM with no
route at all should still fail fast and early). Only its claim that the route gate *also* covers
source reachability is superseded by the new evidence.

## Decision

After `wait_for_agent` and the existing default-route `wait_for_network` gate confirm the build
guest is on the network, `EphemeralBuildVm.session` runs **one bounded in-guest egress preflight
against the configured build source** before yielding the transport — so an unreachable source
fails the gate with a message naming the source, *before* the clone runs.

**1. The preflight is `git ls-remote`, run in-guest over the existing transport.**

- The session runs `git ls-remote --quiet --exit-code <remote> HEAD` via the same
  `GuestExecBuildTransport` (allowlist `{'/bin/sh'}`, unchanged) the clone uses, with a bounded
  per-call timeout. Success (`rc 0`) means the guest resolved DNS, completed the TCP/TLS or SSH
  handshake, and reached the repository — the egress preconditions the immediately-following
  `git fetch --depth 1 <remote> <ref>` needs. Probing the **real remote over the real protocol**
  means the preflight cannot drift from how the clone dials the source.
- The probe targets **`HEAD`, not the configured `ref`**. The clone resolves an *arbitrary
  ref/sha* (`git fetch --depth 1 <remote> <ref>` supports a bare commit SHA). A bare SHA is not an
  advertised ref, so `ls-remote --exit-code <remote> <that-sha>` returns non-zero on a fully
  reachable host — binding the egress check to the configured ref would spuriously fail SHA-pinned
  builds. Probing `HEAD` keeps the check on *egress to the source*; ref existence stays the clone's
  contract, with the fetch's own stderr surfaced for a genuinely bad ref.
- This is a **single bounded attempt**, not a poll loop. The route gate already absorbed
  DHCP-slowness; a source that does not answer a bounded `ls-remote` after the route is up is a
  real reachability/config fault, not a not-yet-ready signal, so retrying would only delay an
  honest failure (and burn the build budget).

**2. Failure category names the cause, with the remote redacted.**

- `git ls-remote` returns 128 for both "could not resolve host / connection refused" (a
  network/egress fault) and "repository not found / bad ref" (a configuration fault), and the
  build VM cannot reliably distinguish them from the exit code alone. We map a failed preflight
  to **`CONFIGURATION_ERROR`** — "build VM cannot reach build source `<remote>`" — and attach the
  probe's **redacted** stderr (`redacted_tail` + `redact_url_credentials` on the named remote) so
  triage sees the underlying git error without a credentialed URL leaking into an error detail.
  `CONFIGURATION_ERROR` is the honest dominant category here: the operator's actionable fix is
  almost always "open the guest-subnet egress / fix DNS / fix the remote," all configuration.
- A `CategorizedError` raised by the transport itself (the agent dropped mid-probe) **propagates
  unchanged** — `wait_for_agent` already confirmed the channel, so a drop is a genuine
  `transport_failure`, identical to how the route gate treats it.

**3. The configured source is threaded into the session; absent → preflight is skipped.**

- The build source (`kernel_source_ref`) is known to the dispatch layer
  (`run_build_on_host` / `_git_coords`) but not to the transport factory. We extend the
  build-host transport-factory contract with an optional `source: GitSourceRef | None` and have
  `run_build_on_host` resolve it from `parsed.kernel_source_ref` **before** opening the factory,
  passing it through to `EphemeralBuildVm.session`. The ephemeral factory forwards it; the SSH
  and local factories ignore it (their host network is already up / not a throwaway guest).
- When `source is None` (a non-git warm-tree source, or any caller that does not supply one), the
  preflight is **skipped** and behavior is exactly ADR-0144's route-only gate — no regression for
  the warm-tree lane, which does no in-guest clone.

## Consequences

- A build VM that has a default route but cannot reach the configured source now fails the
  readiness gate with "build VM cannot reach build source `<remote>`" **before** the clone, instead of
  a confusing downstream `git fetch`/FETCH_HEAD error. The operator sees the unreachable source
  named and the redacted git error.
- A VM with working egress proceeds unchanged: the extra cost is one in-guest `git ls-remote`
  round-trip after the route is up, bounded by a per-call timeout.
- The probe uses the remote's own protocol, so it is correct for https/ssh/git remotes alike and
  does not assume HTTP — directly answering ADR-0144's strongest objection.
- The transport-factory contract gains one optional parameter shared by all three factories
  (SSH/local/ephemeral); only the ephemeral factory acts on it. No new MCP tool, schema field,
  DB column, migration, env-var, or auth-model change. The preflight timeout is a constructor
  default (injectable), mirroring `_NETWORK_TIMEOUT_S`/`_AGENT_TIMEOUT_S`.
- The default-route gate (ADR-0144) is unchanged and still runs first; this is an added, narrower
  precondition, not a replacement.

## Considered & rejected

- **Keep only the default-route gate (status quo, ADR-0144).** Rejected by new field evidence:
  on `ub24-big` the route was present and the host network verified valid, yet the clone failed
  on no egress to the source. "Route up" demonstrably does not imply "source reachable."
- **DNS-resolution-only probe (`getent hosts <remote-host>`).** Rejected for the same reason
  ADR-0144 gave (it needs the host parsed out of the URL) *and* because it under-checks: DNS can
  resolve while the TCP/TLS hop to the source is still policy-dropped. `git ls-remote` covers
  resolve + connect + handshake + ref in one bounded probe without URL parsing.
- **Parse the remote into host:port and TCP-connect.** Rejected: reimplements per-scheme port
  logic (https 443 / git 9418 / ssh 22 / custom ports), is fragile for scp-like `git@host:path`
  remotes, and still would not prove the git handshake works. `git ls-remote` delegates all of
  this to git.
- **Poll/retry the preflight with backoff (like the route gate).** Rejected: the route gate
  already absorbs DHCP-slowness, so a post-route `ls-remote` failure is a real fault; polling it
  only delays an honest error and can burn the build budget before the clone even starts.
- **Categorize the failure as `TRANSPORT_FAILURE`.** Rejected as the default: `git ls-remote`
  cannot distinguish "couldn't resolve/connect" from "no such repo/ref" (both rc 128), and the
  operator's fix for the dominant cause (egress policy / DNS / wrong remote) is configuration.
  `CONFIGURATION_ERROR` with the redacted git stderr is the least-surprising, most-actionable
  mapping; a genuine transport drop (agent gone) still propagates as `transport_failure`.
- **Run the preflight in the dispatch layer (just before the clone) instead of in the session.**
  Rejected to keep the build-VM readiness contract in one place: the session owns "this guest is
  ready to build," and source-reachability from the guest is part of that. Threading the source
  into the session keeps the gate's failure shape (provisioning/config errors tear the VM down)
  consistent with the route gate, rather than splitting readiness across two layers.
