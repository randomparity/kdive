# 0555 — Systemd retains host worker incarnations

## Status

Accepted (2026-08-10)

## Context

ADR-0533 requires an authority to register each worker incarnation before startup, deliver its
unique credential privately, and publish exact termination evidence before its artifact fences can
be recovered. The Compose gate and Kubernetes witness provide that ordering, but the supported
host-process live stack starts workers directly. Mandatory incarnation authentication therefore
makes every host worker exit before it can claim work.

The two live-VM jobs must keep workers on the host because they need the host libvirt socket,
staged images, and provider runtime paths. The self-hosted runner has no sudo during a job, so a
launcher that depends on ad hoc privilege escalation cannot be the supported path. The repair is a
live-stack deployment facility, not a general KDIVE process supervisor.

## Decision

Provision fixed system units `kdive-live-worker@1.service` through
`kdive-live-worker@8.service`. Each instance runs as its own no-login account, uses
`Restart=no`, `KillMode=control-group`, `ExitType=cgroup`, and `RemainAfterExit=yes`, and receives
its incarnation credential through `LoadCredential`. A root-owned environment file supplies only
that slot's immutable local-systemd identity and worker-role runtime settings. Workers never
receive the lifecycle-witness database authority.

A systemd socket activates a request-scoped root lifecycle witness. It accepts only bounded
`start`, `status`, `stop`, and `diagnostics` requests. The socket is `root:kdive-live-control` mode
`0660`, that group contains only the configured operator account, and the service requires the
connection's `SO_PEERCRED` UID to equal that provisioned account before parsing a request. The
caller may supply a worker count and allowlisted unprivileged worker settings, but never a unit
name, command, credential, state path, or lifecycle-witness DSN. One host lock serializes requests.
The service exits after each request; systemd units and root-owned per-slot state retain authority
between requests.

For each slot, the witness mints a random generation and 256-bit credential, atomically publishes
their fixed files, and registers a `local` incarnation bound to the fixed unit and generation. Only
then may it start the unit. It records the unit invocation identifier after activation. Stop sends
SIGTERM to the complete unit cgroup and waits for that exact invocation to become empty. The
witness publishes the mapped terminal outcome through the lifecycle-witness database role before
resetting the unit or removing the credential and state. A database or systemd failure retains
those objects for an idempotent retry; absence alone is never termination evidence.

The host launcher continues to run server and reconciler as ordinary host processes, but gives
each process its role-specific database DSN. It invokes the existing local runtime-role bootstrap
after migrations. The worker accounts share only the provisioned session-libvirt socket and the
provider directories needed by the live topology.

Failure diagnostics are deliberately non-transactional. Before teardown, the fixed diagnostics
request reads only the current worker units, removes the retained credential and worker DSN
literals, bounds the output, and returns it to the caller. Both live workflows print that output in
an `if: failure() || cancelled()` step before any cleanup. The lifecycle repair does not promise
post-stop augmentation or durable cross-run journal archives.

## Consequences

Supported host-process operation now requires systemd system units, provisioned worker accounts,
the control socket, and a group-accessible session-libvirt socket. Local operators install that
host contract explicitly; the self-hosted runner receives it from Ansible and hosted CI installs it
on its disposable VM.

An unresolved stop can retain a failed unit, credential, and active incarnation row. This is the
intentional fail-closed result: the next `status`, `stop`, or `start` reconciles the same slot before
replacement. Force removal remains an operator recovery that may strand fences and cannot create
termination evidence.

The request-scoped witness does not monitor workers continuously. Unexpected exits remain retained
by systemd and are evidenced on the next lifecycle or diagnostics request. This may delay recovery,
but it cannot authorize recovery early.

## Considered & rejected

- **A long-running host orchestration daemon.** Continuous monitoring shortens recovery latency but
  adds run ownership, helper supervision, and crash-recovery state unrelated to restoring startup.
- **Direct launcher processes or user systemd units.** The worker would share the invoking account's
  authority and could escape or interfere with the evidence boundary; the current direct path is
  also the defect being repaired.
- **Passwordless sudo commands from the launcher.** The self-hosted job intentionally has no sudo,
  and command-level sudo either exposes too much authority or recreates a control protocol in
  sudoers text.
- **Move the worker into Compose.** The live jobs require host session-libvirt and staged host
  artifacts that the application image does not carry or mount.
