# 0555 — Systemd supervises host worker incarnations

## Status

Accepted (2026-08-10)

## Context

ADR-0533 requires every worker to authenticate with a unique authority-registered incarnation
credential and names the Compose lifecycle gate and Kubernetes witness as the only supported
runtime authorities. The local live-stack and both live-VM jobs instead run workers directly as
host processes. They neither register an immutable runtime identity nor deliver a credential, so a
worker exits before it can claim jobs. The launcher also gives every process the shared development
database principal.

Both live jobs use non-root workers and session libvirt, but a process launched directly under the
workflow account is not isolated from that account's sudo or Docker authority. A host-root worker
cannot be isolated from any co-resident witness. A custom PID/socket supervisor would need to
reimplement process-tree ownership, crash adoption, peer admission, and secret isolation already
provided by the system service manager.

## Decision

This decision partially supersedes ADR-0533's two-authority exclusivity by adding the systemd system
manager plus the local lifecycle witness as the authority for non-root host workers. ADR-0533's
credential, ordering, role-separation, termination-evidence, and artifact-fence requirements remain
unchanged.

Host lifecycle startup rejects `KDIVE_WORKER_AS_ROOT=1` before launching an application process. The
error directs the operator to the provisioned non-root worker and its explicit session-libvirt URI.
A future root-mode replacement requires a separate decision and constrained provider privilege
boundary.

Each configured worker uses one installed `kdive-live-worker@<slot>.service` instance. Every slot
in the bounded range has its own no-login `kdive-worker-<slot>` account with no sudo or Docker
access and read-only code and configuration. Distinct UIDs prevent one worker from reading another
unit's systemd credential directory. The unit uses `Restart=no`, `KillMode=control-group`,
`ExitType=cgroup`, `RemainAfterExit=yes`, and `StartLimitIntervalSec=0`. It therefore retains an
exact named runtime object after its complete cgroup becomes empty, never restarts silently, and
cannot reject an exact-generation witness retry because of a service start-rate limit.

The worker incarnation identity and authority binding contain the bounded unit name and a random
per-start generation. The lifecycle witness mints a random 256-bit credential, durably writes the
root-only credential source and a `prepared` state file containing the binding and hash, and then
registers those exact facts and the current fence protocol through the lifecycle-witness database
role. It persists `registered` only after the transaction commits, then verifies that the fixed
unit is inactive with neither a pending job nor an invocation identifier and durably records
`starting` with the current host boot identifier before asking systemd to start it. Systemd first
exposes an accepted asynchronous request as the unit's pending start job, then assigns an invocation
identifier when activation begins. The witness waits for the exact start job and records the
invocation identifier and `started` phase. Systemd `LoadCredential=` copies the source into that
slot UID's service credential directory. The worker reads that private copy and authenticates
through the worker database role. No application process receives both roles or the source
credential file.

A crash in `prepared` retries registration with the same generation, binding, hash, and credential;
the existing registration function accepts an exact replay of an active incarnation. A crash after
the database commit but before `registered` is persisted follows the same replay path. A crash in
`registered` advances the same generation rather than minting a replacement. After a crash in
`starting`, the witness adopts and waits for the unit's pending start job; a non-empty invocation
identifier is re-adopted after the job advances. On the same host boot, an inactive unit with no job
and no invocation has no submitted or running invocation, so the witness retries the same
generation. A pending non-start job, changed boot identifier, missing unit, or contradictory unit
state is ambiguous and fails closed.

The lifecycle witness is a system service with `Restart=on-failure`. It owns only the witness DSN,
credential source files, lifecycle state, and authority to inspect and operate the fixed worker
template instances. The trusted root deployment launcher prepares separate root-owned environment
files containing each process's role-specific DSN; it exits before workers start. Worker units
receive only the worker environment file. The witness receives no worker environment file.

The server and reconciler also run as separate no-login `kdive-server` and `kdive-reconciler`
accounts with no sudo or Docker access. Each receives only its own environment file and database
role. The sudo-capable workflow or operator account performs bounded provisioning and observation,
then exits; it does not host a long-running KDIVE application process.

For normal stop, the witness sends SIGTERM to the unit cgroup without unloading the unit, waits for
the cgroup to become empty, compares the retained unit name, generation, and systemd invocation
identifier with its root-owned state, and records terminal evidence before stopping/resetting the
unit and deleting the credential source and lifecycle state. A clean spontaneous exit is retained
as `active (exited)` by `RemainAfterExit=yes`; a non-zero exit, fatal signal, timeout, watchdog, or
OOM is retained as `failed`. Either is terminal evidence only when the exact unit, generation,
invocation identifier, and empty cgroup match.

The witness maps a clean `success` result to `succeeded`, `exit-code` or `resources` with a matching
invocation to `failed`, and the bounded signal, core-dump, timeout, watchdog, and OOM-kill results to
`killed`. A `resources` failure with no invocation and no pending job did not create a runtime; on
the same boot, the witness resets only that failed unit state and retries the same registered
generation. `start-limit-hit` is impossible for the provisioned template and therefore proves unit
drift; it and every unknown result fail closed without resetting the unit. The outcome is
diagnostic, while the empty exact cgroup plus matching unit, generation, and invocation identifier
is the termination authority for an invocation that ran.

If the witness crashes, systemd retains the worker unit, cgroup, generation state, and credential
source. The restarted witness reconciles every configured slot before opening a new generation. A
live cgroup is re-adopted. An empty matching cgroup receives terminal evidence before cleanup. A
missing, mismatched, multiply active, or prematurely cleaned unit fails closed and leaves the
incarnation and artifact uses pinned. The explicit force-cleanup path may strand a fence but cannot
publish evidence or release one.

A separate no-login `kdive-libvirt` account owns one persistent session-libvirt daemon and explicit
Unix socket. The slot worker accounts, reconciler, and trusted live-test account use the same
`qemu+unix:///session?socket=...` URI through a bounded socket group; the accounts do not share a
login UID. Staged images and provider runtime paths use declared setgid ownership. Host preflight
proves socket and path access from every configured consumer identity before application startup.

The host stack applies migrations with the migration-owner login, runs the idempotent runtime-role
bootstrap, and starts server, reconciler, lifecycle witness, and worker units with only their
role-specific database authorities. Each worker slot keeps its existing distinct health port.

## Consequences

Host workers now have registration-before-start and evidence-before-cleanup ordering analogous to
the Compose gate. One and several workers receive separate UIDs, credentials, generations, systemd
units, cgroups, environment files, and log files.

Supported live-stack hosts must run the systemd system manager and provision the bounded slot,
server, reconciler, and libvirt accounts; session-libvirt daemon and socket; lifecycle directories;
units; and narrowly scoped launcher authority. Hosted CI installs that boundary during setup; the
self-hosted runner declares it in Ansible. A host without it fails preflight instead of falling
back to direct process launch.

The previous root-worker and invoking-user worker paths are no longer supported live-stack modes.
Root-only local-libvirt operations need a separate privileged interface. User-systemd services
remain available for ordinary deployments but are not an ADR-0533 lifecycle authority.

Local development retains fixed allowlisted passwords split among migration, server, worker,
reconciler, and witness logins. External operators continue to supply their own role DSNs and
disable local role bootstrap.

## Considered & rejected

- **Keep direct host processes and add a custom guardian.** That recreates process-tree containment,
  privilege drop, connection admission, crash adoption, and durable runtime identity already owned
  by the system service manager.
- **Use the invoking account's user-systemd manager.** The worker would share the account that can
  control its manager and, in CI, administer sudo or Docker, so the witness would not be
  independent.
- **Keep the root worker beside a witness.** A compromised root worker can inspect or replace every
  co-resident authority.
- **Give one application process both runtime DSNs.** A compromise could act as the worker and
  publish its own termination evidence, recreating the boundary ADR-0536 removed.
- **Share a static credential or make it optional.** Either path lets an unregistered or unrelated
  incarnation claim jobs and defeats ADR-0533.
- **Run the worker in the managed Compose service.** That container lacks the host libvirt socket,
  staged images, runtime paths, and provisioned toolchain required by the live topology.
