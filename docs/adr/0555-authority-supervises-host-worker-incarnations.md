# 0555 — Authority supervises host worker incarnations

## Status

Accepted (2026-08-10)

## Context

ADR-0533 requires every worker to authenticate with a unique authority-registered incarnation
credential and names the Compose lifecycle gate and Kubernetes witness as the only supported
runtime authorities. The local live-stack and both live-VM jobs instead run workers directly as
host processes. They neither register the immutable local process identity nor deliver a
credential, so the worker exits before it can claim jobs. The same launcher gives all processes the
shared development database principal and therefore bypasses the runtime-role split.

The host topology is required: its local-libvirt worker needs the host's libvirt socket, staged
images, runtime directories, and, on the native runner, the provisioned Python toolchain. Both live
jobs already use a non-root worker with `qemu:///session`. The launcher's root-worker mode cannot
provide an authority independent from a compromised worker: host root can inspect or replace any
co-resident witness. It must not be presented as an ADR-0533-preserving topology.

## Decision

This decision partially supersedes ADR-0533's two-authority exclusivity by adding the non-root local
host lifecycle witness. ADR-0533's credential, ordering, role-separation, termination-evidence, and
artifact-fence requirements remain unchanged.

Host lifecycle startup rejects `KDIVE_WORKER_AS_ROOT=1` before launching any application process.
The error directs the operator to an unprivileged worker under `qemu:///session`. A future root-mode
replacement requires a separate decision and a constrained privilege boundary; it cannot reuse this
host-process authority.

The host stack runs one dedicated local lifecycle-witness daemon plus one worker-side guardian for
each configured worker. The daemon receives only the lifecycle-witness database DSN. Each worker
receives only the worker database DSN. The guardian retains neither database authority after
starting its child. No long-running process receives both runtime authorities.

The guardian starts one blocked worker under a bounded slot and retains a pidfd. Before releasing
the child, it authenticates a one-use launch capability to the witness and transfers that exact
pidfd with `SCM_RIGHTS`. The non-dumpable worker then connects directly to the witness. The witness
requires its `SO_PEERCRED` PID and immutable start tuple to match the pre-registered pidfd. It mints
a random 256-bit credential and registers the identity and credential hash. Only after registration
commits does it send the credential directly over that peer-bound connection. The worker reads the
bounded credential once before authenticating through the worker role; neither the guardian nor a
sibling receives it.

The credential is never placed in argv, an environment value, a guardian message, or a shared
filesystem path. The witness socket lives in a supervisor-owned `0700` runtime directory and
accepts each configured slot and launch capability once. A slot cannot receive credential bytes
until its worker peer matches the guardian-supplied pidfd. Compose and Kubernetes workers retain
their existing private file handoffs; the peer-bound socket is an additive local-supervisor
transport, not an optional authentication path. A worker without either authority-delivered
transport still fails startup.

The worker keeps its witness connection open for its lifetime and exits if that connection closes.
The witness holds its pidfd while the guardian waits without reaping the exact child. On pidfd
termination the witness records the same immutable binding and a terminal outcome. It accepts the
guardian's exit status only after independently observing termination; that status affects
diagnostics, not whether the death is authoritative. A transient database failure keeps the witness
retrying and prevents a graceful host-stack restart or teardown from claiming completion.

The artifact-fence holder remains the worker process named by that immutable identity. Supported
provider operations keep protected consumption in worker threads or wait for synchronous child
commands before returning; they may not detach an artifact-consuming descendant from the worker.
This is the same process-death boundary ADR-0533 protects, not a new process-tree recovery claim.

The witness holds a database advisory singleton lock while serving a launch generation. If it
crashes, connection loss stops the worker and the guardian retains the terminated child as an
unreaped process plus its pidfd. It does not reconnect while its child can execute. The launcher
starts one replacement witness, which acquires the singleton lock before binding the socket. Each
guardian then transfers its retained pidfd and holder for terminal-only adoption. The witness reads
the registered authority binding, compares it with the still-inspectable PID and start tuple,
observes pidfd termination, and commits evidence before acknowledging reap or opening a replacement
slot. Duplicate, live, mismatched, or already-reaped adoptions fail closed.

If the guardian disappears, a parent-death signal set before credential delivery terminates the
child and the surviving witness observes its pidfd. Simultaneous force loss of both lifecycle
processes can strand a fence, but cannot publish false termination evidence or release one.

The local stack applies migrations with the migration-owner login, runs the existing idempotent
runtime-role bootstrap, and launches server, worker, reconciler, guardian, and witness with only
their role-specific database authorities. The short-lived operator launcher selects which DSN each
process receives, removes every unrelated role DSN before execution, and supplies a unique one-use
capability for each configured guardian slot.

## Consequences

Host workers now have the same registration-before-authentication and evidence-before-cleanup
ordering as the other supported deployments. Multiple workers receive separate credentials and
immutable identities without contending on `/run/kdive/worker-incarnation-credential`.

The host launcher gains one witness daemon and a guardian process for each worker. Graceful restart
and teardown may fail while a worker is still finishing or termination evidence cannot reach
Postgres. Operators may restore the database and retry; witness loss or force teardown remains an
explicit fail-closed bypass that can leave artifact uses pinned for later diagnosis.

Local development retains fixed allowlisted passwords, but they are divided among the migration,
server, worker, reconciler, and witness logins and are never suitable for external deployment.
External operators continue to supply their own role DSNs and disable the local role bootstrap.

The prior root-worker default is no longer a supported live-stack path. Operators use the non-root
session-mode configuration already exercised by both live jobs. Root-only local-libvirt operations
need a separately designed privileged interface rather than weakening worker-fence authority.

## Considered & rejected

- **Share the fixed credential file among host workers.** Concurrent workers can overwrite or read
  another incarnation's credential, and file cleanup cannot identify which process still owns the
  path.
- **Give every worker a configurable private credential file.** This can separate paths, but it
  persists credential bytes and still needs an additional start barrier to ensure registration
  commits before the worker reads. A peer-bound socket supplies both properties directly.
- **Make credentials optional for local workers.** This recreates the bypass ADR-0533 removed and
  lets a host worker claim jobs without an authority-bound identity.
- **Give one supervisor both runtime DSNs.** A process compromise could use worker authority and
  publish its own termination evidence, recreating the dual-authority boundary ADR-0536 removed.
- **Keep the root worker and trust it beside the witness.** A compromised host-root worker can read,
  trace, signal, or replace the witness, so process names and separate DSNs do not form an authority
  boundary.
- **Run the worker in the existing managed Compose service.** The container lacks the host libvirt,
  staged-image, runtime-directory, and native toolchain access that the live-VM topology requires.
- **Have the launcher register a PID after starting an ordinary worker.** The worker can read its
  handoff and attempt authentication before registration, making startup depend on a race.
