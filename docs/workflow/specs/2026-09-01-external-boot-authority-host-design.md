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
an authority-owned journal, and a distinct dormant authority-owned libvirt endpoint that fixed
workers and the reconciler cannot access. The current fixed-worker endpoint, KVM access, and
provider behavior remain unchanged until #2140 can replace them atomically. Readiness remains
false until the service proves its journal directory, credential, database role, dormant provider
endpoint, and denial boundary.

The deployment does not register an external-boot provider capability. Existing production
composition tests continue to prove that `external_boot_authority_v1` is absent.

## Components and data flow

### Authority host runtime

`kdive.providers.external_boot_authority.host` provides two narrow commands behind the existing
`python -m kdive` entry point:

- `external-boot-authority-host` loads only systemd credentials and fixed configuration, checks
  the local journal and access boundary, verifies its database session is a member of only the
  least-privilege `kdive_provider_authority` role, and repeats the full check on a fixed interval.
  Any drift exits the process so process liveness retracts readiness and systemd restarts it.
- `check-external-boot-authority-host` runs the same checks once and emits one bounded,
  secret-free readiness result for Ansible and operators.

The service does not accept authority mutation requests in this issue. #2140 owns binding real
provider commit points and the mutually authenticated request transport. Starting the host process
without that adapter is deliberately deployment readiness, not capability readiness.

### Host identities and paths

Ansible creates these independent principals and groups:

- `kdive-provider-authority` owns the service, journal, service credential, database credential,
  installed runtime, and its distinct dormant libvirt session.
- Existing fixed workers keep their current `kdive-live-libvirt` and `kvm` memberships and their
  current provider endpoint until #2140 supplies a complete replacement.
- `kdive-provider-authority` is the only KDIVE service identity able to traverse the new dormant
  authority endpoint.

The authority runtime root is `/opt/kdive-provider-authority`; credentials are under
`/etc/kdive/credentials/provider-authority`; journals are under
`/var/lib/kdive/provider-authority/journal`; runtime state is under
`/run/kdive/provider-authority`. Each protected parent is inspected without following links before
Ansible creates or changes it.

The authority account runs a separate session libvirtd under
`/run/kdive/provider-authority/libvirt`. Its Unix-user session is a separate provider namespace
from the existing operator-owned fixed-worker daemon, so current workers cannot observe or mutate
authority-owned objects. The authority daemon is installed dormant: #2140 must bind provider
adapters, prove mutation exclusivity, and only then advertise capability or retire current access.

### Database authority

Migrations 0122 and 0123 already create `kdive_provider_authority`, revoke direct table access,
and grant only the binding-resolution, acknowledgement, and journal-head functions accepted by
ADR-0584. Migration 0125 adds one security-definer inventory function: it authenticates the
calling role, accepts one configured authority instance, and returns only the bounded lane identity
and exact trusted-head fields needed for restoration. It grants execution only to
`kdive_provider_authority`; no direct table access or lifecycle write is added. Provisioning
installs a login DSN whose role is a member of that existing role. The readiness probe checks role
shape and never prints the DSN.

### Readiness

The one-shot and long-running probes fail closed unless all of these hold:

1. the process identity is the configured authority uid;
2. credentials are regular, non-symlink files owned by that uid with mode `0400`;
3. the database inventory and confined local journal lanes are a bijection, and every retained
   lane is a real authority-owned directory/file whose exact terminal record equals its trusted
   head;
4. the configured provider mutation socket is a socket reachable by the authority identity;
5. the database session has the accepted role shape and can execute only the authority functions;
6. the configured worker and reconciler identities cannot traverse the authority runtime,
   credentials, journal, helper, or mutation socket paths; and
7. the existing fixed-worker provider/KVM path remains usable and unchanged.

The runtime writes no durable readiness override. It repeats every check at the configured bounded
interval and exits on any drift; #2140 must also call the same check immediately before enabling an
adapter or admitting its first request. Journal restoration is therefore a prerequisite: any
corrupt, foreign, missing, extra, longer, or valid-prefix-truncated lane fails the inventory/local
bijection or trusted-head comparison before readiness.

## Threat model

### Boundary inventory

The added boundaries are systemd credential delivery into the authority process, the authority
database connection, the authority journal filesystem, and a distinct authority-owned libvirt
session. No network listener or caller-selected path, URI, command, provider definition, or
credential is added. The existing fixed-worker endpoint is unchanged.

### Actors and controls

- A compromised fixed worker or reconciler retains existing-provider access but must not reach the
  dormant authority endpoint's credentials, socket, helper, journal, runtime, or provider objects.
  Distinct Unix identities/session namespaces, ownership, and negative Ansible probes enforce this.
- A compromised tenant cannot select any host path or credential because the host commands accept
  configuration only from root-owned files and systemd credential descriptors.
- A local platform administrator remains trusted, matching ADR-0584. Root can bypass filesystem
  ACLs and is explicitly outside this boundary.
- A stale authority process cannot claim readiness from a cached stamp: startup and every periodic
  interval reconstruct journal, database, provider, and access evidence, then exit on drift.
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
- Structural deployment tests prove account/session separation, installed modes, systemd
  supervision, credential delivery, and absence of worker/reconciler authority-endpoint access.
- The authorized Ubuntu 26.04 x86_64 carrier executes the role from a clean KDIVE state, starts and
  restarts both endpoints, runs positive/negative readiness probes, injects drift, and verifies
  readiness retracts. Native provider behavior remains the later #2151/#2152 proof.
- Provider-neutral adversarial tests explicitly cover unresolved calls across takeover/restart,
  valid-prefix journal loss, independent lane heads, and stale provider/core writes.
- Existing composition tests continue to prove capability advertisement is disabled.
- Focused tests, `just lint`, whole-tree `just type`, relevant hooks, and `just ci` gate delivery.

## Rollback

Reverting removes the dormant authority unit and migration function while leaving the current
fixed-worker provider path untouched. Because provider capability advertisement remains disabled,
rollback has no active external-boot operation to migrate. Journal and credential directories are
retained rather than deleted; an operator may inspect or remove them only after confirming no later
#2140 deployment uses them.
