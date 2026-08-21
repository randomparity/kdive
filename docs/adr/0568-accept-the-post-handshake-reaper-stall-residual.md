# 0568 — Accept the post-handshake reaper stall residual bounded by the lane budget

## Status

Accepted (2026-08-21)

## Context

ADR-0565 bounds the two host-state reaping lanes (capture and dump-volume) against a host that never answers the TCP handshake: `open_libvirt_reaper` gates on a bounded TCP connect, so an unreachable declared host costs `KDIVE_REMOTE_LIBVIRT_CONNECT_TIMEOUT_SECONDS` (default 5 s) instead of the kernel's ~130 s SYN retry budget.

That gate does not bound a host that **accepts** the TCP connection and then stalls — in the TLS handshake, or in a live-but-wedged libvirtd's RPC (a storage call blocked on NFS, say). Such a host still holds the reaping lane's transaction, and therefore the per-job ownership fence (ADR-0556) or the `(SYSTEM, system_id)` advisory lock (ADR-0562), for as long as it stalls.

ADR-0565 deferred two escalation options "for now":

1. **`virConnectSetKeepAlive` on the reaper connection.** Libvirt's keepalive mechanism detects a peer that stops answering and fails pending RPCs, which is exactly the post-handshake stall the TCP gate does not cover. ADR-0565 rejected it on two grounds: it needs the server side to have keepalive enabled and reports failure — potentially closing the connection — against a peer that does not, which is a fail-closed change to every existing deployment's reaper path; and it detects a *dead* peer, not a live libvirtd whose storage call is blocked.

2. **Extending ADR-0558's supervised child process to reaping.** The supervised process shape bounds a live-but-blocked peer at an OS process boundary, which the other escalation cannot. ADR-0565 rejected it as genuinely more than the hazard needs: a supervisor, a quiescence protocol, and a spool per reaper call, for a lane whose dominant failure is a host that never answers.

What limits the blast radius today is ADR-0565's per-lane budget: both lanes check `_budget_spent` **between** candidates, never while a provider call is in flight. A host that stalls after accepting the connection therefore costs the pass one candidate, not the whole batch. The lane returns after the stalled candidate completes, logs the count it left unattempted, and re-derives the rest on the next pass.

## Decision

Accept the post-handshake stall residual permanently, bounded by the lane budget that ADR-0565 already introduced.

The lane budget is the correct bound for this hazard. It caps each stalled host at one candidate per pass, preserving ADR-0556's ownership contract — the budget check is outside the transaction, so it can never end a transaction while a provider call may still be mutating host state. The residual is already bounded to one candidate per pass; the deferred escalations would add heavy machinery for a blast radius that is already acceptable.

Neither escalation is worth its cost. Keepalive does not help the actual stall shape: it detects a *dead* peer, not a live libvirtd whose storage call is blocked, and it requires a fail-closed deployment change that may close working connections on existing deployments. Supervised child process is the correct shape for bounding a live-but-blocked peer, but it is disproportionate for a hazard whose dominant failure is a host that never answers at all — which ADR-0565's TCP gate already bounds.

The per-candidate blast radius is acceptable. A host that stalls after the handshake costs the pass one candidate and holds the lane's transaction for as long as it stalls, but it does not block the entire batch and it does not violate the ownership contract. An operator whose backlog is not draining raises the budget or the reconcile interval; the hazard already has two knobs tuned for the operator's situation.

## Consequences

- A post-handshake stall is bounded to one candidate per pass. The lane returns after the stalled candidate completes, logs the count it left unattempted, and re-derives the rest on the next pass. This is the same behavior the lane already has when a candidate fails; the only difference is that a stalled candidate may take longer to complete.
- The ownership contract remains intact. The budget check is outside the transaction, so it can never end a transaction while a provider call may still be mutating host state. ADR-0556's requirement that the fence be held across the call is preserved.
- No new settings, no migration, no schema change. The lane budget (`KDIVE_RECONCILER_LANE_BUDGET_SECONDS`, default 10 s) is the operator's existing knob for this hazard.
- The two deferred escalations remain rejected permanently. ADR-0565's alternatives section updates to point at this record rather than leaving them as "for now" deferrals.
- The INFO line naming the unattempted count remains the whole of the signal a truncated lane leaves. Making it observable beyond that log line remains #1982, as ADR-0565 already noted.

## Considered & rejected

- **Add `virConnectSetKeepAlive` on the reaper connection.** Detects a *dead* peer, not a live libvirtd whose storage call is blocked, which is the actual stall shape reaping lanes meet. It also requires a fail-closed deployment change that may close working connections on existing deployments, for a hazard whose blast radius is already bounded by the lane budget. The cost outweighs the benefit.

- **Extend ADR-0558's supervised child process to reaping.** The correct shape for bounding a live-but-blocked peer, but disproportionate for this hazard. Adding a supervisor, a quiescence protocol, and a spool per reaper call would be heavy machinery for a problem whose dominant failure is a host that never answers at all — which ADR-0565's TCP gate already bounds. The lane budget already caps the residual at one candidate per pass.

- **Add a per-host RPC timeout via `asyncio.timeout` or `asyncio.wait_for`.** The provider call runs in a synchronous libvit client wrapped in `asyncio.to_thread`, so cancelling the await abandons the worker thread rather than stopping it. The fenced transaction would then end — releasing the ownership fence or the System lock — while the provider call was still mutating host state, violating ADR-0556.

- **Do nothing and leave the residual unbounded.** ADR-0565 already rejected this: an untunable ~130 s per unreachable host, up to 25 times per pass, makes the capture sweep's pacing assumption false on exactly the fleets the sweep exists for. The post-handshake stall is unbounded only in the sense that it can take longer than the connect timeout; the lane budget already bounds the blast radius to one candidate per pass.
