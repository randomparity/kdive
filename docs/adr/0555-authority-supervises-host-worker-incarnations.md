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
images, runtime directories, and, on the native runner, the provisioned Python toolchain. It must
support several concurrent worker processes without sharing a credential or handoff path.

## Decision

This decision partially supersedes ADR-0533's two-authority exclusivity by adding the local host
lifecycle witness. ADR-0533's credential, ordering, role-separation, termination-evidence, and
artifact-fence requirements remain unchanged.

The host stack runs one dedicated local lifecycle-witness daemon plus one worker-side guardian for
each configured worker. The daemon receives only the lifecycle-witness database DSN. Each guardian
and its child receive only the worker database DSN. No long-running process receives both runtime
authorities.

The guardian creates a one-shot pipe and starts the worker blocked on its read descriptor. Through
a private local control socket it gives the witness an exact pidfd for that child under a bounded,
one-use worker slot. The witness derives the child's immutable identity from hostname, PID, boot ID,
and process start ticks, mints a random 256-bit credential, and registers the identity and
credential hash. Only after registration commits does it return the credential to the guardian for
delivery through the pipe. The worker reads the bounded credential once and closes the descriptor
before authenticating through the worker role.

The descriptor is inherited only by that child. The credential is never placed in argv, an
environment value, or a shared filesystem path. The witness control socket lives in a
supervisor-owned `0700` runtime directory and accepts each configured slot once. Compose and
Kubernetes workers retain their existing private file handoffs; the descriptor input is an additive
local-supervisor transport, not an optional authentication path. A worker without either
authority-delivered transport still fails startup.

The witness holds the pidfd while the guardian waits for its exact child. On pidfd termination the
witness records the same immutable binding and a terminal outcome. It accepts the guardian's exit
status only after independently observing termination; that status affects diagnostics, not whether
the death is authoritative. A transient database failure keeps the witness retrying and prevents a
graceful host-stack restart or teardown from claiming completion.

The artifact-fence holder remains the worker process named by that immutable identity. Supported
provider operations keep protected consumption in worker threads or wait for synchronous child
commands before returning; they may not detach an artifact-consuming descendant from the worker.
This is the same process-death boundary ADR-0533 protects, not a new process-tree recovery claim.

The guardian keeps its control connection open for the worker lifetime. If the witness disappears,
the guardian terminates and reaps its child. If the guardian disappears, a parent-death signal set
before credential delivery terminates the child and the surviving witness observes the pidfd. A
witness crash can still lose the only authoritative handle and strand a fence; launch refuses a
replacement while the corresponding worker or guardian remains, and the explicit force-stop path
cannot publish false termination evidence or release the fence.

The local stack applies migrations with the migration-owner login, runs the existing idempotent
runtime-role bootstrap, and launches server, worker, reconciler, guardian, and witness with only
their role-specific database authorities. The short-lived operator launcher selects which DSN each
process receives and removes every unrelated role DSN before execution.

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

## Considered & rejected

- **Share the fixed credential file among host workers.** Concurrent workers can overwrite or read
  another incarnation's credential, and file cleanup cannot identify which process still owns the
  path.
- **Give every worker a configurable private credential file.** This can separate paths, but it
  persists credential bytes and still needs an additional start barrier to ensure registration
  commits before the worker reads. A one-shot inherited pipe supplies both properties directly.
- **Make credentials optional for local workers.** This recreates the bypass ADR-0533 removed and
  lets a host worker claim jobs without an authority-bound identity.
- **Give one supervisor both runtime DSNs.** A process compromise could use worker authority and
  publish its own termination evidence, recreating the dual-authority boundary ADR-0536 removed.
- **Run the worker in the existing managed Compose service.** The container lacks the host libvirt,
  staged-image, runtime-directory, and native toolchain access that the live-VM topology requires.
- **Have the launcher register a PID after starting an ordinary worker.** The worker can read its
  handoff and attempt authentication before registration, making startup depend on a race.
