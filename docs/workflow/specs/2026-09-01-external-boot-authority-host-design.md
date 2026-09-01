# External-boot authority host deployment design

## Scope and authority

Issue #2150 deploys the provider-host authority boundary accepted by
[ADR-0584](../../adr/0584-provider-host-authority-fences-external-boot-mutations.md). The
deployment must be installable on a clean live-VM runner and must expose only deployment
readiness. Provider adapters and capability advertisement remain owned by #2140; lifecycle
orchestration remains owned by #2118; native proofs remain owned by #2151 and #2152.

The implementation targets x86_64 and ppc64le. It uses Python 3.14, systemd, Ansible, PostgreSQL,
and the existing provider-neutral authority service and journal.

## Outcome

A provisioned host has a dedicated `kdive-provider-authority` identity, an authenticated and
supervised authority process, a root-installed runtime, an authority-only database credential,
an authority-owned journal, and a mutation-capable libvirt socket that fixed workers and the
reconciler cannot access. Fixed workers keep read-only libvirt observation through the daemon's
read-only socket. Readiness remains false until the service proves its journal directory,
credential, database role, provider socket, and denial boundary.

The deployment does not register an external-boot provider capability. Existing production
composition tests continue to prove that `external_boot_authority_v1` is absent.

## Components and data flow

### Authority host runtime

`kdive.providers.external_boot_authority.host` provides two narrow commands behind the existing
`python -m kdive` entry point:

- `external-boot-authority-host` loads only systemd credentials and fixed configuration, checks
  the local journal and access boundary, verifies its database session is a member of only the
  least-privilege `kdive_provider_authority` role, and stays supervised while ready.
- `check-external-boot-authority-host` runs the same checks once and emits one bounded,
  secret-free readiness result for Ansible and operators.

The service does not accept authority mutation requests in this issue. #2140 owns binding real
provider commit points and the mutually authenticated request transport. Starting the host process
without that adapter is deliberately deployment readiness, not capability readiness.

### Host identities and paths

Ansible creates these independent principals and groups:

- `kdive-provider-authority` owns the service, journal, service credential, database credential,
  installed runtime, and libvirt mutation group.
- `kdive-live-observe` grants fixed workers read-only access to `libvirt-sock-ro`.
- `kdive-provider-authority` is the only KDIVE service identity in the libvirt mutation group.

The authority runtime root is `/opt/kdive-provider-authority`; credentials are under
`/etc/kdive/credentials/provider-authority`; journals are under
`/var/lib/kdive/provider-authority/journal`; runtime state is under
`/run/kdive/provider-authority`. Each protected parent is inspected without following links before
Ansible creates or changes it.

The dedicated session libvirtd publishes its read-write socket to the authority group and its
read-only socket to the observation group. Fixed worker units lose `kvm` and mutation-group
membership; the runner account remains an operator outside the fixed worker/reconciler boundary.

### Database authority

Migrations 0122 and 0123 already create `kdive_provider_authority`, revoke direct table access,
and grant only the binding-resolution, acknowledgement, and journal-head functions accepted by
ADR-0584. Provisioning installs a login DSN whose role is a member of that existing role. The
readiness probe checks membership and rejects superuser, role creation, database creation,
replication, bypass-RLS, and direct journal-table privileges. It never prints the DSN.

### Readiness

The one-shot and long-running probes fail closed unless all of these hold:

1. the process identity is the configured authority uid;
2. credentials are regular, non-symlink files owned by that uid with mode `0400`;
3. the journal root and every retained lane are real authority-owned directories/files with the
   exact modes required by `FileAuthorityJournal`;
4. the configured provider mutation socket is a socket reachable by the authority identity;
5. the database session has the accepted role shape and can execute only the authority functions;
6. the configured denial identities cannot traverse the authority runtime, credentials, journal,
   helper, or mutation socket paths; and
7. the read-only provider socket is reachable by a fixed observation identity and rejects a
   mutation operation in the clean-host verification carrier.

The runtime writes no durable readiness override. Restart repeats every check. Journal restoration
is therefore a prerequisite: any corrupt, foreign, missing, longer, or valid-prefix-truncated lane
causes `FileAuthorityJournal.load()` or the trusted-head comparison to fail before readiness.

## Threat model

### Boundary inventory

The added boundaries are systemd credential delivery into the authority process, the authority
database connection, the authority journal filesystem, and the split read-only/read-write libvirt
sockets. No network listener or caller-selected path, URI, command, provider definition, or
credential is added. The existing libvirt socket boundary is narrowed for fixed workers.

### Actors and controls

- A compromised fixed worker or reconciler may read provider state but must not reach mutation
  credentials, socket, helper, journal, or runtime. Unix ownership, non-overlapping groups,
  systemd unit groups, and negative Ansible probes enforce this.
- A compromised tenant cannot select any host path or credential because the host commands accept
  configuration only from root-owned files and systemd credential descriptors.
- A local platform administrator remains trusted, matching ADR-0584. Root can bypass filesystem
  ACLs and is explicitly outside this boundary.
- A stale authority process cannot claim readiness from a cached stamp: every start reconstructs
  journal and database evidence.
- A malicious or replaced path component cannot redirect privileged writes: provisioning and the
  runtime reject symlinked or foreign-owned protected paths.

### Failure disclosure

Readiness errors name a bounded component and reason, never a path supplied by an untrusted caller,
credential content, DSN, provider output, or journal bytes. systemd restarts the service on
failure; Ansible stops provisioning when the one-shot probe or any negative access proof fails.

### Explicitly out of scope

This design does not protect against root, implement mTLS/request dispatch, bind local- or
remote-libvirt provider primitives, advertise a provider capability, or orchestrate external-boot
lifecycle state. Those are either accepted exclusions or owned by #2140/#2118.

## Verification

- Host-runtime tests exercise safe path/credential checks, role-shape failures, journal restore
  failures, and bounded diagnostics.
- Structural deployment tests prove account/group separation, installed modes, systemd
  supervision, credential delivery, read-only worker URI, and absence of mutation access.
- Provider-neutral adversarial tests explicitly cover unresolved calls across takeover/restart,
  valid-prefix journal loss, independent lane heads, and stale provider/core writes.
- Existing composition tests continue to prove capability advertisement is disabled.
- Focused tests, `just lint`, whole-tree `just type`, relevant hooks, and `just ci` gate delivery.

## Rollback

Reverting removes the authority unit and restores the prior fixed-worker libvirt group shape.
Because provider capability advertisement remains disabled, rollback has no active external-boot
operation to migrate. Journal and credential directories are retained rather than deleted; an
operator may inspect or remove them only after confirming no later #2140 deployment uses them.
