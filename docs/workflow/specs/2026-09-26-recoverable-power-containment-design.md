# Operator power-call quiescence feasibility

## Problem and scope

Issue #2836 follows #2831: the System fence protects force-crash ordering while provider I/O
runs, but a libvirt call that does not return can retain that fence and the worker cancellation
path indefinitely. ADR-0689 defines a proof gate before changing this behavior. This issue
delivers an ADR and spec only: no runtime, schema, test, deployment, or tool change.

The investigation covers ON, OFF, CYCLE, RESET, and RESUME on supported local and remote
libvirt paths. OFF's clean-shutdown wait remains #2801; its underlying RPCs are in scope.
Force-crash marker/NMI rules, install and external-boot shutdown, reconciler reaping, and
unrelated provider operations remain with their owners. Remote RESUME currently falls through
to `reboot` in its controller; #2838 owns that existing defect. Remote RESUME containment
cannot be enabled before #2838 lands.

## Verified failure model

- `src/kdive/jobs/handlers/control/control.py` runs provider power calls in a thread under a
  session advisory lock. Repeated cancellation waits for thread completion; there is no
  provider-call deadline.
- Local and remote libvirt controllers open a connection, look up a domain, and call provider
  methods. OFF additionally probes state, requests shutdown, waits by ADR-0685's policy, then
  may destroy. The wait policy does not bound a stalled libvirt RPC.
- [Libvirt RPC internals](https://libvirt.org/kbase/internals/rpc.html) and its
  [12.0.0 server source](https://github.com/libvirt/libvirt/blob/v12.0.0/src/rpc/virnetserver.c)
  show parallel dispatch and a worker-queued request retaining its client after receipt. The
  [server-client close path][client]
  drains socket queues, not server worker jobs. This is evidence that client exit is not a
  universal cancellation witness.
- [QEMU domain job rules](https://libvirt.org/kbase/internals/qemu-threads.html) and the
  [12.0.0 QEMU driver](https://github.com/libvirt/libvirt/blob/v12.0.0/src/qemu/qemu_driver.c)
  show serialization once a call enters its domain job. They do not show that a newer query
  cannot overtake an older call still waiting to dispatch. A fresh query alone is no barrier.

## Proof contract

For each action/provider pair, a proposed non-reboot barrier passes only when all of these are
shown for the deployed libvirt version and transport:

1. Bind an exact power attempt, domain identity, provider endpoint, and client execution
   boundary. Observe exact client absence before relying on any post-client barrier. A missing
   worker heartbeat, expired lease, lost database lock, PID alone, or closed socket is not
   process-absence evidence.
2. Prove the barrier acknowledges only after any earlier accepted power RPC can no longer make
   a later provider mutation. Cover a call queued but not yet dispatched, one waiting to enter
   a domain job, one running in that job, and a downstream QEMU/agent command. State separately
   what a synchronous RPC return proves and what later guest behavior remains asynchronous.
3. Exercise the proof with a real local or remote libvirt provider by delaying the accepted
   call at each relevant boundary and racing the candidate barrier. The barrier may neither
   acknowledge early nor silently turn an unobservable attempt into success. Fakes may test
   state-machine behavior but cannot establish actual provider ordering.
4. Define the recovery authority and evidence that a replacement can verify after worker loss.
   Bind evidence to the same domain and provider epoch; reject missing, stale, or mismatched
   evidence. A targeted domain/daemon epoch counts only if source and live proof exclude old
   queued dispatch and downstream effects. Host reboot is excluded.

The design matrix starts with every pair marked unknown. Each feasibility follow-up changes
only the pair it actually investigates. The action-specific proof obligations include:

| Action | Provider work whose late execution must be excluded |
| --- | --- |
| ON | open, lookup, create, and any accepted start work |
| RESET | open, lookup, reset, and QEMU monitor reset |
| CYCLE | open, lookup, reboot request; subsequent guest behavior retains current semantics |
| RESUME | open, lookup, resume, before PAUSED-to-READY commit; remote also depends on #2838 |
| OFF | open, lookup, state/shutdown/wait probes/destroy/close; clean wait stays #2801 |

Local and remote receive separate verdicts. An unproved pair keeps its current ADR-0687
unbounded call path and lock. No finite provider deadline or kill-and-unlock path is enabled
there. An inconclusive attempt is not success. The investigation must say whether a pair is
feasible under this contract; an unknown result is not treated as passing.

## Conditional implementation surfaces

If at least one pair passes, a later design may specify an exact gated child, durable pending
attempt, shared System admission guard, late-result handling, authority-bound recovery, and an
agent-facing pending status. Those are **provisional surfaces**, not approved schema or work
items. The later design must inventory every `LockScope.SYSTEM` site and identify provider
mutation or its state-changing admission, including control, boot, teardown, snapshot/restore,
external boot, and provider reaping paths. It must split implementation into reviewable PRs
only after the passing pair and its barrier are known. No unproved pair is upgraded as a side
effect.

The numeric parent deadline and termination grace are not chosen in this design PR. A later
implementation must derive them from live latency evidence and state the full limit contract:
unit, monotonic reference clock, per-attempt scope, pending consequence, and recovery action.
A child stuck in uninterruptible I/O may survive SIGKILL; a finite parent wait does not imply
finite child resource use or permission to clear its System fence.

## First follow-up boundary

The first investigation takes local-libvirt RESET, a single synchronous mutating RPC, and one
concrete non-reboot barrier candidate found in the deployed libvirt source. It must first show
that the candidate closes the queued-dispatch gap; if source disproves that, report no-go
without building a live harness for it. If source remains plausible, a disposable-VM live test
delays an accepted RESET while the candidate barrier races it. This follow-up changes no power
runtime, database schema, or tool contract, and claims no other action or remote transport.
A passing pair leads to a new operator-reviewed implementation plan and PR-sized work items
filed by campaign root. A failed or unknown pair remains on ADR-0687 behavior and records the
exact missing proof. Later investigations are split by candidate and action/provider evidence,
not pre-filed as a ten-pair batch. Tests need x86_64 and ppc64le target evidence before that
pair's deadline is enabled; the current development host is x86_64.

[client]: https://github.com/libvirt/libvirt/blob/v12.0.0/src/rpc/virnetserverclient.c
