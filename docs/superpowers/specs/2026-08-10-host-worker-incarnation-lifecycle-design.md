# Host worker incarnation lifecycle design

Issue: #1926

Decision: [ADR-0555](../../adr/0555-systemd-supervises-host-worker-incarnations.md)

Scope: `WORK:SCOPE` token `4f128723-af12-4c57-a749-c20a2cdf9c30`

## Goal and boundary

Restore the non-root host-process live stack by starting each worker through a systemd-backed
lifecycle witness. Every worker must authenticate a registered unique incarnation, use the worker
database role, and retain exact termination evidence before its credential is removed.

This design covers worker units, the minimum privileged witness, host launcher wiring, runtime-role
bootstrap, shared session-libvirt access, focused proofs, and pre-teardown failure diagnostics. It
does not convert server or reconciler into system units, create a general run orchestrator, add
privileged prepare/report/cleanup helpers, guarantee post-stop diagnostics, or test every individual
filesystem persistence instruction. Those exclusions are part of the operator-approved rescope.

## Approaches

### Selected: request-scoped witness and retained worker units

Systemd retains the exact worker cgroup and invocation after the request-scoped root witness exits.
Small root-owned per-slot records hold only the facts needed to replay registration, start, and
termination. This supplies the ADR-0533 authority boundary without introducing another continuously
running application supervisor.

### Rejected: long-running lifecycle supervisor

A daemon can notice unexpected exits immediately, but it needs its own restart reconciliation,
run ownership, report ordering, and helper lifecycle. Delayed evidence is safe because fences stay
pinned; immediate recovery is not required to restore the live jobs.

### Rejected: direct or Compose worker launch

Direct launch cannot keep lifecycle-witness authority outside the worker account or bind positive
evidence to a retained cgroup. Compose is already an authority, but the live jobs deliberately need
host libvirt and host-staged provider artifacts.

### Selected deployment compatibility: use each distro's packaged daemon model

The lifecycle contract requires one operator-owned, group-accessible session-libvirt endpoint; it
does not require one daemon binary or socket basename on every distro. Provisioning follows the
repository's existing libvirt split: Debian-family systems use monolithic `libvirtd` and
`libvirt-sock`, while Red Hat-family systems use modular `virtqemud` and `virtqemud-sock`. A
root-owned, non-secret environment file publishes the selected URI to the launcher, which passes it
into worker configuration. Provisioning fails before worker activation when the selected daemon is
not installed. It does not create a compatibility socket alias or run both daemon models.

## Host contract

Provisioning installs:

- `kdive-live-worker@.service` and no-login accounts `kdive-worker-1` through
  `kdive-worker-8`;
- `kdive-live-worker-lifecycle.socket` and its request-scoped service;
- root-owned `/var/lib/kdive/live-workers/slots/<n>/` state directories;
- a fixed live-stack control group containing the configured operator or runner account;
- a shared group for the worker accounts, operator, session-libvirt socket, rootfs directory, and
  install-staging directory; and
- the installed lifecycle entrypoint and worker wrapper as root-owned, non-writable executables.

The hosted job installs the same files on its disposable VM. The `live_vm_host` Ansible role owns
the persistent self-hosted installation. `up.sh` fails before migration or process launch when the
socket, accounts, installed version, directory permissions, or shared libvirt access are absent. It
also rejects execution as root and removes `KDIVE_WORKER_AS_ROOT` as a supported mode. Before
activation, the witness scans live processes for `kdive worker` commands outside its fixed unit
cgroups and fails with their bounded identities; it never adopts or kills an unmanaged process.

The operator starts the target distro's packaged session daemon with an explicit socket beneath the
provisioned group-traversable runtime directory: `libvirtd` on Debian-family systems and
`virtqemud` on Red Hat-family systems. Worker accounts connect to the URI published by
provisioning; QEMU remains owned by the operator so the live suite can read its console and
artifacts. No worker receives sudo, Docker, the control group, or another slot's primary group.

## Control boundary

The socket is `root:kdive-live-control` mode `0660`. Provisioning makes the configured operator the
group's only member and stores that account's numeric UID in root-owned service configuration. The
service reads `SO_PEERCRED` and rejects every other UID before parsing bytes. It then accepts one
bounded JSON request. Operations are `start`, `status`, `stop`, and `diagnostics`. A non-blocking
root-owned lock rejects concurrent operations with an actionable retry message.

One live-stack flow owns a host from `up` through `down`. The hosted job has a disposable host and
the native job's non-cancelling workflow concurrency serializes flows on its persistent host. Local
operators must provide the same whole-flow serialization; `start` replaces the current fleet and
`stop` targets the current fleet. The request lock prevents simultaneous mutation but does not
create per-flow ownership. Concurrent local stacks require separate hosts.

`start` accepts a worker count in `1..8` plus an allowlist of worker runtime values already needed
by the live stack: absolute Python and source paths, worker database DSN, libvirt URI, provider
directories, object-store settings, accepted lanes, and per-slot health binds. A request is at most
32 KiB and each string is at most 4 KiB; an over-limit or malformed request makes no state change
and tells the caller to correct and retry it. The witness validates path type, ownership, and
writability before copying values into fixed per-slot environment files. It derives every unit
name, state path, identity, generation, and credential itself. Other operations accept no mutable
runtime configuration. Every operation has a 120-second per-request deadline measured by the
service's monotonic clock. A deadline aborts child operations, retains unresolved state, and tells
the caller which operation to retry.

The lifecycle-witness DSN is a root-only systemd credential installed by provisioning. It is never
read from the request, worker environment, checkout, or invoking process. Responses contain only
operation status, fixed slot numbers, bounded errors, and, for `diagnostics`, sanitized text.

## Per-slot lifecycle

Each slot has one root-owned state document and fixed credential and environment files. The state
contains schema version, slot, fixed unit, random generation, derived incarnation ID, credential
hash, phase, and optional host boot ID, systemd invocation ID, and terminal outcome. Phases are
`prepared`, `gated`, `registered`, `started`, and `terminated`. State replacements are write-file,
`fsync`, atomic rename, and parent-directory `fsync`; there is no run-wide record or receipt graph.

The incarnation ID is `local-systemd:<unit>:<generation>`. The database authority kind remains
`local`, with binding containing the unit, generation, host boot ID, and systemd invocation ID.
Worker identity loading accepts this configured local-systemd identity only from the root-owned
unit environment; ordinary local PID identity remains available to existing non-systemd callers
but is not a supported live worker authority.

The fixed unit starts a root-installed gate wrapper as the slot UID. The wrapper reads only its
fixed environment and waits for a root-owned release marker in the slot runtime directory; the
slot UID can read but not create or replace that marker. The marker contains exactly the generation
and invocation ID. The wrapper compares those values with its environment generation and systemd's
`INVOCATION_ID`; a stale or malformed marker cannot release it. It imports no checkout or KDIVE code
before release. After release it removes no state and `exec`s the configured Python worker in the
same cgroup and systemd invocation. The witness retains and later removes the marker with the rest
of that exact generation only after terminal evidence commits.

### Start

`start(count)` first runs the stop/evidence flow over every occupied slot in `1..8`, including a
slot above a reduced count. An unresolved slot blocks every new activation. Once all old generations
are cleared, the witness starts exactly slots `1..count`. For each requested slot, it:

1. requires an inactive fixed unit and no unresolved earlier state;
2. publishes a new environment, credential, absent release marker, and `prepared` state;
3. starts the fixed gate wrapper, then records the current boot ID and exact invocation as `gated`;
4. registers that incarnation, binding, and credential hash through the witness role;
5. persists `registered` after the transaction commits;
6. atomically publishes the root-owned release marker; and
7. verifies the same invocation has `exec`d a live worker and persists `started`.

Registration is idempotent for identical facts. Before `gated`, an inactive unit with no invocation
retries the same prepared generation; an active gate is adopted and its exact invocation recorded.
A crash after the database commit replays the same registration. A crash around release sees either
the still-gated wrapper or the worker in the same registered invocation and resumes accordingly. A
contradictory or foreign invocation fails closed. A partial multi-worker start stops and evidences
already started or registered slots; it does not mint replacement generations in the same request.

### Status and unexpected exit

`status` reports each configured slot from its state and exact systemd properties. A live cgroup is
running. An empty retained invocation is terminal; the witness maps the unit result to `succeeded`,
`failed`, or `killed` and publishes termination through the witness role. It persists `terminated`
after the database transaction commits but retains the unit and credential until `stop`, so later
diagnostics can redact the credential. Unit absence, a generation mismatch, an unavailable system
manager, or an unreadable cgroup never becomes evidence.

The witness is not a monitor. An unexpected exit can remain unevidenced until `status`, `stop`,
`diagnostics`, or the next `start`; the active database fence remains pinned during that delay.
State also records the boot ID. If the current boot differs, the old invocation and every process
it contained are definitively gone; the witness commits that exact binding as `killed` before
cleanup. An unreadable or unchanged boot ID never licenses this transition.

### Stop and cleanup

`stop` sends SIGTERM to every `gated`, `registered`, or `started` unit cgroup, then gives all
selected cgroups 45 seconds on the monotonic clock, within the 120-second request budget, to become
empty. It records each terminal outcome and then stops/resets only the fixed unit and deletes the
environment, credential, and state after the database confirms the same incarnation is terminated.
A `prepared` state with positive proof that no invocation was created needs no database termination
and is discarded before cleanup. Repeating `stop` adopts the same facts.

If systemd, the database, or the worker does not converge within the bound, `stop` fails and keeps
the unit, state, credential, and incarnation row. The operator retries after restoring the
dependency. A separate force procedure may kill or remove host objects but is explicitly unable to
write termination evidence and may strand artifact fences.

## Launcher and database roles

After applying migrations, `up.sh` invokes the existing local runtime-role bootstrap. Local
defaults use the existing development member accounts; external operators may supply distinct
role-member DSNs. The migration DSN remains only in the migration/bootstrap commands.

Server and reconciler remain ordinary non-root host processes. Their launch environments replace
`KDIVE_DATABASE_URL` with the server and reconciler member DSNs. The worker request similarly maps
the worker member DSN to its fixed unit environment. Only the root lifecycle service receives the
witness member DSN.

`restart_host_processes` first asks the witness to stop any retained worker slots, then restarts
server and reconciler, asks the witness to start the configured workers, and waits for exactly those
slots. `status.sh` combines ordinary server/reconciler process health with lifecycle `status`.
`down.sh` stops workers through the witness before stopping the remaining host processes and
backends. A failed worker stop blocks destructive backend teardown unless the operator explicitly
chooses the existing force path and accepts stranded fences.

Before `restart_host_processes` asks `start` to replace an occupied managed fleet, it runs
`diagnostics` and prints the bounded redacted snapshot. Failure to capture a safe snapshot aborts
bring-up without stopping or resetting the old fleet. This is ordinary launcher ordering under the
single-flow-per-host contract; it adds no durable report state.

## Failure diagnostics

`diagnostics` is observational: it neither changes lifecycle state nor writes database evidence.
For each current slot it reads only the journal entries for the recorded unit invocation plus a
fixed allowlist of unit properties. The request schema classifies every worker setting as public
or secret. Diagnostics replaces the exact retained credential and every delivered secret setting,
including database and object-store credentials, before emitting text, applies structural
URL-userinfo and secret-key redaction,
escapes control characters, and emits at most 256 KiB per slot and 1 MiB per request. Acquisition
uses a 30-second monotonic budget and reads at most 320 KiB per slot and 1.25 MiB total before
redaction. Reaching a time or byte ceiling stops acquisition and emits one truncation marker from
the safely redacted material; the caller may retry. Unsafe state or an oversized redaction source
withholds that slot rather than returning unredacted content.

Both live jobs add a separate `if: failure() || cancelled()` step after their existing live-test
step and before any cleanup. It invokes the diagnostics script and prints the bounded output with
workflow-command interpretation disabled. Diagnostic failure reports a short warning and does not
replace the original job failure. Clean teardown, post-stop augmentation, and durable report
manifests are not part of this issue.

## Threat model

### Actors and trust

- The local operator or CI runner is trusted to select worker code and unprivileged runtime
  settings. It is not trusted with lifecycle-witness database authority.
- Worker code is untrusted with respect to termination evidence and sibling credentials.
- Root provisioning and systemd are the host authority.
- PostgreSQL is the artifact-fence enforcement boundary from ADR-0533.

### Boundaries and controls

- **Operator to root socket:** mode-`0660` dedicated-group DAC, exact provisioned-UID comparison
  with `SO_PEERCRED` before parsing, one bounded schema, allowlisted worker settings, derived
  privileged names, and serialized requests.
- **Root witness to worker:** distinct UID per slot, fixed unit, systemd credential handoff,
  root-owned environment/state, cgroup containment, and no witness DSN.
- **Witness to PostgreSQL:** root-only witness credential and exact incarnation/binding calls.
- **Workers to shared libvirt:** group access only to the explicit session socket and required
  provider directories; no operator login identity, control socket, sudo, or Docker access.
- **Journald to workflow log:** fixed unit/invocation selection, literal secret removal, control
  escaping, and byte ceilings before emission.

Out of scope are a malicious root or provisioner, a compromised kernel/systemd/PostgreSQL server,
force cleanup that strands fences, continuous worker monitoring, and durable diagnostics after
teardown. Two sequential trusted local flows on one host are also out of scope: the later operation
intentionally targets the current fleet. The design does not weaken behavior when any named
component is unavailable.

## Acceptance proofs

1. Worker identity tests reject malformed configured local-systemd IDs and prove the authenticated
   database incarnation must equal the configured ID.
2. Lifecycle unit tests prove unique credentials and generations, register-before-start,
   gate-before-registration and release-after-registration, exact identity handoff, gated/start
   adoption, stale-marker refusal, exact boot/cgroup/invocation checks, reboot outcome, mapped
   outcomes,
   evidence-before-cleanup, idempotent registration/termination replay, whole-fleet count
   convergence, unmanaged-worker refusal, partial-start cleanup, bounds, and fail-closed dependency
   behavior. Limit tests use a fake monotonic clock and prove request, stop, diagnostic acquisition,
   and emitted-byte ceilings retain retryable state.
3. Unit-shape and provisioning tests pin fixed commands, per-slot UIDs, `LoadCredential`,
   `Restart=no`, `KillMode=control-group`, `ExitType=cgroup`, socket permissions, root-only witness
   configuration, `RemainAfterExit=yes`, the root-owned release marker, no application import before
   release, shared libvirt access, and absence of worker sudo/Docker/control membership.
4. Script tests prove migration then role bootstrap ordering, role-specific daemon DSNs, removal of
   direct/root worker launch, exact worker counts, lifecycle start/status/stop wiring, and refusal
   to tear down backends after unresolved evidence. Replacement tests prove diagnostics print
   before the first stop/reset and a diagnostic failure leaves the occupied fleet untouched.
5. A disposable-Postgres process proof starts one and several real worker units through the
   lifecycle seam, observes distinct active incarnations and worker-role connections, terminates
   them, and observes exact terminal rows. A systemd-hosted proof exercises the installed units
   where the host supports it.
6. Workflow tests prove both live jobs reach their existing test commands and run bounded redacted
   diagnostics on failure or cancellation before cleanup. Reporter tests cover bare credentials,
   every secret request-field class, hostile control text, missing journals, output bounds, and no
   lifecycle or database writes.
7. Focused Python, shell, systemd, Ansible, and workflow checks pass, followed by `just ci`.

## Rollback

Before reverting, run lifecycle `stop` and confirm every slot is terminated and its credential is
removed. Reverting the units and launcher cannot restore direct workers because mandatory
incarnation authentication remains. If stop cannot prove termination, retain the host state and
repair the dependency rather than removing it as part of rollback.
