# 0689 — Prove provider quiescence before bounding operator power calls

## Status

Accepted (2026-09-26)

Extends [ADR-0687](0687-fence-power-provider-io.md). The OFF clean-shutdown wait remains
[ADR-0685](0685-operator-power-off-clean-shutdown.md)'s decision.

## Context

ADR-0687 holds a System advisory lock through each `control.power` provider call and waits
through task cancellation. A libvirt call that never returns can retain the lock and worker
cancellation path indefinitely. A timeout around `asyncio.to_thread` would leave a mutating
provider thread alive after the lock is released. A supervised client process can bound the
parent's wait, but client termination alone cannot prove that a request already accepted by
libvirt has stopped.

[Libvirt's RPC design][rpc] permits parallel server workers. In the
[libvirt 12.0.0 server source][server],
a received request enters a worker queue with a reference to its client; the worker dispatches
it without a closed-socket check. Closing the client drains its receive/transmit queues, not
that worker queue, in the [server-client source][client]. [QEMU driver job rules][qemu-threads]
serialize work after it enters a domain job, but do not establish ordering between an older
queued RPC and a new query on another connection. The source-backed conclusion is that neither
client exit nor a fresh domain-state query is a universal quiescence witness.

## Decision

Do not enable a finite provider-call deadline for an operator power action on a local or remote
libvirt path until a provider-specific recovery barrier is positively proved. The proof must
cover the exact action's connection, lookup, and mutating RPCs; OFF also covers state probes,
shutdown, and hard destroy while leaving its clean wait policy intact. It must exclude a late
mutation from an RPC accepted before the client's timeout, including one still queued for
server dispatch or active in QEMU or a guest agent. Source reasoning for the deployed libvirt
version and a live test that delays an accepted RPC while a recovery probe attempts to overtake
it are both required. The same proof must be established for local and remote transport where
each is enabled. Fake-only tests cannot establish provider ordering.

A targeted domain or daemon epoch may serve as a candidate barrier only if its evidence proves
that the old request cannot resume after the barrier acknowledges completion. A daemon restart
or domain-state observation alone is not proof. Hypervisor-host reboot is excluded as a
recovery path by operator decision. If a barrier cannot be proved for an action/provider pair,
keep ADR-0687's existing unbounded wait and System fence for that pair. Any later bounded
path must retain an inconclusive attempt as fail-closed; it cannot report success or admit
another provider mutation based on missing evidence. This ADR does not authorize a timeout,
process supervisor, schema, or shared admission change before that feasibility result exists.

A first, PR-sized follow-up investigates one concrete candidate on local-libvirt RESET, a
single-RPC mutating path. It first checks the deployed libvirt source for a barrier
that closes queued dispatch; only a source-plausible candidate proceeds to a disposable-VM
delayed-dispatch live test. It records a pass, fail, or unknown verdict for that pair. The
remaining action/provider pairs are not presumed equivalent and receive separate bounded
investigations only if evidence warrants them. Remote RESUME enablement also depends on
#2838's mapping repair. Any later implementation design is scoped only for pairs with a
passing proof. Its numeric deadline and termination grace come from live latency evidence and
must state the
unit, monotonic reference clock, per-attempt scope, pending consequence, and recovery action in
the agent-facing contract.

## Consequences

- This design issue alone changes no runtime behavior. No finite provider deadline is promised
  for an unproved libvirt path.
- A successful feasibility result permits a later bounded design to specify durable ownership,
  System admission, exact client supervision, pending status, and recovery. Those surfaces are
  provisional until the barrier and its deployment authority are proved.
- A failed or unavailable barrier leaves the existing #2831 wait in place. The feasibility
  report names the unsupported action/provider pair rather than promising an unsafe timeout.
- The investigation must account for x86_64 and ppc64le targets; this host is x86_64. A local
  proof does not imply remote TLS ordering, or the converse.

## Considered & rejected

- **Cancel `asyncio.to_thread` at a deadline.** Python cannot stop its running provider thread.
- **Kill a client and query domain state.** The queued server request can execute later.
- **Assume domain job locking orders separate RPC clients.** Locking begins after dispatch;
  an older queued call can be overtaken.
- **Use hypervisor-host reboot as recovery.** The operator excluded it for this issue.

[rpc]: https://libvirt.org/kbase/internals/rpc.html
[server]: https://github.com/libvirt/libvirt/blob/v12.0.0/src/rpc/virnetserver.c
[client]: https://github.com/libvirt/libvirt/blob/v12.0.0/src/rpc/virnetserverclient.c
[qemu-threads]: https://libvirt.org/kbase/internals/qemu-threads.html
