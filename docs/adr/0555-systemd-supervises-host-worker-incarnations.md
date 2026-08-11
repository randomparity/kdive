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

The supported topology has one live-stack flow per host. The configured operator serializes the
whole interval from `up` through `down`; hosted jobs get a fresh host and the persistent native job
keeps its existing non-cancelling workflow concurrency. The request lock prevents simultaneous
mutations but is not a cross-flow lease. `start` deliberately replaces the host's current worker
fleet and `stop` deliberately stops the current fleet, so overlapping local flows are unsupported.

For each slot, the witness mints a random generation and 256-bit credential, atomically publishes
their fixed files, derives `local-systemd:<unit>:<generation>`, and places that exact non-secret ID
in the per-start environment handoff. It registers the same incarnation, bound to the fixed unit
and generation, before starting the unit; worker authentication must return that ID. It records the
unit invocation identifier after activation. Stop sends SIGTERM to the complete unit cgroup and
waits for that exact invocation to become empty. The witness publishes the mapped terminal outcome
through the lifecycle-witness database role before resetting the unit or removing the credential
and state. A database or systemd failure retains those objects for an idempotent retry; absence
alone is never termination evidence.

`start(count)` first scans for live `kdive worker` processes outside the fixed unit cgroups and
refuses to activate anything while one exists. It then applies the same evidence-before-cleanup
flow to every occupied slot in `1..8`, including slots above a reduced count, and starts exactly
slots `1..count`. Any unresolved termination blocks all new activation. The launcher has no root or
direct-worker option and reports an outsider without adopting or killing it.

Each request is at most 32 KiB and has a 120-second deadline measured on the service's monotonic
clock. Stop signals all selected cgroups and gives them 45 seconds within that per-request budget to
empty. Timeout returns an error and retains every unresolved unit, generation, credential, and
fence; the operator may run `diagnostics` or `status`, restore the dependency, and retry `stop`.
Diagnostics has a 30-second acquisition budget, reads at most 320 KiB per slot and 1.25 MiB total,
and emits at most 256 KiB per slot and 1 MiB total with a truncation marker. An over-limit request
is rejected without state change; an acquisition limit returns only safely redacted bounded output
and may be retried.

The host launcher continues to run server and reconciler as ordinary host processes, but gives
each process its role-specific database DSN. It invokes the existing local runtime-role bootstrap
after migrations. The worker accounts share only the provisioned session-libvirt socket and the
provider directories needed by the live topology.

Failure diagnostics are deliberately non-transactional. Before teardown, the fixed diagnostics
request reads only the current worker units. Every allowlisted worker setting is classified public
or secret, and the response removes every delivered secret literal, including the incarnation,
database, and object-store credentials, before bounding output. Both live workflows print that
output in an `if: failure() || cancelled()` step before any cleanup. The lifecycle repair does not
promise post-stop augmentation or durable cross-run journal archives.

## Consequences

Supported host-process operation now requires systemd system units, provisioned worker accounts,
the control socket, and a group-accessible session-libvirt socket. Local operators install that
host contract explicitly; the self-hosted runner receives it from Ansible and hosted CI installs it
on its disposable VM.

An unresolved stop can retain a failed unit, credential, and active incarnation row. This is the
intentional fail-closed result: the next lifecycle request reconciles the same slot before
replacement. Force removal remains an operator recovery that may strand fences and cannot create
termination evidence.

The request-scoped witness does not monitor workers continuously. Unexpected exits remain retained
by systemd and are evidenced on the next lifecycle or diagnostics request. This may delay recovery,
but it cannot authorize recovery early.

The design does not protect two trusted local operators from sequentially replacing or stopping
each other's live stack. That host is a single-flow development resource; callers needing concurrent
stacks use separate hosts.

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
