# 0555 — Authority supervises host worker incarnations

## Status

Accepted (2026-08-10)

## Context

ADR-0533 requires every worker to authenticate with a unique authority-registered incarnation
credential and recognizes the Compose lifecycle gate and Kubernetes witness as supported runtime
authorities. The local live-stack and both live-VM jobs instead run workers directly as host
processes. They neither register the immutable local process identity nor deliver a credential, so
the worker exits before it can claim jobs. The same launcher gives all processes the shared
development database principal and therefore bypasses the runtime-role split.

The host topology is required: its local-libvirt worker needs the host's libvirt socket, staged
images, runtime directories, and, on the native runner, the provisioned Python toolchain. It must
support several concurrent worker processes without sharing a credential or handoff path.

## Decision

Each host worker is the child of a dedicated local lifecycle supervisor. The supervisor creates a
one-shot pipe, starts the worker with only the read descriptor, derives the child's immutable local
identity from hostname, PID, boot ID, and process start ticks, and registers that identity plus the
credential hash through the lifecycle-witness database role. Only after registration commits does
the supervisor write the random 256-bit credential to the pipe. The worker reads the bounded
credential once and closes the descriptor before authenticating through the worker role.

The descriptor is inherited only by that child. The credential is never placed in argv, an
environment value, or a shared filesystem path. Compose and Kubernetes workers retain their
existing private file handoffs; the descriptor input is an additive local-supervisor transport, not
an optional authentication path. A worker without either authority-delivered transport still fails
startup.

The supervisor waits for its exact child. On child exit it records the same immutable binding and a
terminal outcome through the witness role before it exits. A transient database failure keeps the
supervisor retrying and prevents a graceful host-stack restart or teardown from claiming completion.
The explicit force-stop path may kill that witness and strand a fence, but cannot publish false
termination evidence or release one.

The local stack applies migrations with the migration-owner login, runs the existing idempotent
runtime-role bootstrap, and launches server, worker, reconciler, and supervisor with only their
role-specific database authorities. The supervisor receives the worker and witness DSNs, then
removes the witness DSN from the child environment before launch.

## Consequences

Host workers now have the same registration-before-authentication and evidence-before-cleanup
ordering as the other supported deployments. Multiple workers receive separate credentials and
immutable identities without contending on `/run/kdive/worker-incarnation-credential`.

The host launcher gains a supervisor process for each worker. Graceful restart and teardown may
fail while a worker is still finishing or termination evidence cannot reach Postgres. Operators may
restore the database and retry; force teardown remains an explicit fail-closed bypass that can leave
artifact uses pinned for later diagnosis.

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
- **Run the worker in the existing managed Compose service.** The container lacks the host libvirt,
  staged-image, runtime-directory, and native toolchain access that the live-VM topology requires.
- **Have the launcher register a PID after starting an ordinary worker.** The worker can read its
  handoff and attempt authentication before registration, making startup depend on a race.
