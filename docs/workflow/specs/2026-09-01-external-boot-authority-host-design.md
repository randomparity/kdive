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
an authority-owned journal, a mutually authenticated request socket, and a distinct dormant
authority-owned libvirt endpoint that fixed workers and the reconciler cannot access. The current
fixed-worker endpoint, KVM access, and provider behavior remain unchanged until #2140 can replace
them atomically. Readiness remains false until the service proves its journal directory,
credentials, database role, authenticated request boundary, dormant provider endpoint, and denial
boundary.

The deployment does not register an external-boot provider capability. Existing production
composition tests continue to prove that `external_boot_authority_v1` is absent.

## Components and data flow

### Authority host runtime

`kdive.providers.external_boot_authority.host` provides two narrow commands behind the existing
`python -m kdive` entry point and owns a transport wrapper around the existing provider-neutral
service:

- `external-boot-authority-host` loads only systemd credentials and fixed configuration, checks
  the local journal and access boundary, verifies its database session is a member of only the
  least-privilege `kdive_provider_authority` role, binds the configured Unix-stream request socket,
  sends systemd `READY=1` only after the first complete check, and repeats the full check every 30
  seconds. Any drift sends `STOPPING=1` and exits, so readiness retracts before systemd restarts the
  process.
- `check-external-boot-authority-host` runs the same checks once and emits one bounded,
  secret-free readiness result for Ansible and operators.

The request socket carries TLS 1.3 over an `AF_UNIX` stream. The server presents an
authority-instance certificate and requires a client certificate chained to the configured worker
client CA. A four-byte network-order length prefix bounds each closed canonical JSON envelope to
1,048,576 bytes before allocation. The envelope contains exactly an operation discriminator, one
existing canonical `external-boot-authority-v1` request, and one worker-incarnation credential of
at most 4,096 UTF-8 bytes. The host hashes the credential, resolves it through the least-privilege
database function, and constructs `AuthenticatedPeer` only for an active fence-protocol-4 worker.
It never logs or echoes the credential. The server reads one frame and writes one closed bounded
response before closing. A success response contains exactly `status: ok` plus the existing
acknowledgement or observation value; an error response contains exactly `status: error` plus one
bounded category from `unauthenticated`, `invalid-request`, `provider-not-configured`,
`superseded`, `journal-conflict`, or `provider-conflict`.

In #2150's dormant configuration every fully authenticated request receives one bounded
`provider-not-configured` response before the `ExternalBootAuthorityService` or a provider adapter
is called. Invalid TLS peers close before reading an envelope; invalid credentials receive only an
`unauthenticated` response. #2140 may inject its concrete adapter into this already authenticated
dispatcher only after the before-use readiness check succeeds. Starting this host is therefore
deployment readiness, never capability readiness, and it cannot acknowledge takeover or mutate a
provider in this issue.

### Host identities and paths

Ansible creates these independent principals and groups:

- `kdive-provider-authority` owns the service, journal, service credential, database credential,
  installed runtime, and its distinct dormant libvirt session.
- `kdive-provider-authority-client` owns traversal of the request-socket parent. The carrier proof
  creates one non-service proof identity in this group and gives only that identity a short-lived
  client certificate and an active test worker-incarnation credential.
- Existing fixed workers keep their current `kdive-live-libvirt` and `kvm` memberships and their
  current provider endpoint until #2140 supplies a complete replacement; neither they nor the
  reconciler join the authority-client group or receive its TLS material.
- `kdive-provider-authority` is the only KDIVE service identity able to traverse the new dormant
  authority endpoint.

The authority runtime root is `/opt/kdive-provider-authority`; credentials are under
`/etc/kdive/credentials/provider-authority`; journals are under
`/var/lib/kdive/provider-authority/journal`; runtime state is under
`/run/kdive/provider-authority`; the request socket is
`/run/kdive/provider-authority/request/authority.sock`. Each protected parent is inspected without
following links before Ansible creates or changes it.

The authority account runs a separate session libvirtd under
`/run/kdive/provider-authority/libvirt`. Its Unix-user session is a separate provider namespace
from the existing operator-owned fixed-worker daemon, so current workers cannot observe or mutate
authority-owned objects. The authority daemon is installed dormant: #2140 must bind provider
adapters, prove mutation exclusivity, and only then advertise capability or retire current access.

### Database authority

Migrations 0122 and 0123 already create `kdive_provider_authority` and grant the accepted read-only
binding and acknowledgement access plus journal-head functions. That shared capability role is
intentionally authority-role-wide, not tenant- or instance-confidential. Migration 0125 adds two
security-definer functions. The inventory function authenticates role membership, accepts one
configured authority instance, and returns at most 4097 bounded lane identities and trusted-head
fields. Row 4097 is the over-limit signal; readiness fails closed above the fixed ceiling of 4096
retained lanes. The SQL function hard-codes `LIMIT 4097` at this trust boundary rather than
accepting a caller-selected bound. The peer-authentication function accepts only a 32-byte worker
credential hash and returns only the matching active fence-protocol-4 incarnation identifier. Both
grant execution only to `kdive_provider_authority`; no raw credential, new direct table access, or
lifecycle write is added. Provisioning installs a LOGIN DSN whose role is a member of that existing
role. The readiness probe checks role shape and never prints the DSN.

### Readiness

The one-shot and long-running probes fail closed unless all of these hold:

1. the process identity is the configured authority uid;
2. the database and TLS private credentials are regular, non-symlink files owned by that uid with
   mode `0400`, and the public certificate/CA files are exact-mode regular files;
3. the database inventory and confined local journal lanes are a bijection, and every retained
   lane is a real authority-owned directory/file whose exact terminal record equals its trusted
   head;
4. the configured provider mutation socket is a socket reachable by the authority identity;
5. the database session has the accepted role shape and can execute only the authority functions;
6. the request listener is bound with the configured authority-instance certificate, TLS 1.3
   minimum, mandatory client-certificate verification, and exact socket ownership/mode;
7. the configured worker and reconciler identities cannot traverse the authority runtime,
   credentials, journal, helper, request socket, or mutation socket paths; and
8. the existing fixed-worker provider/KVM path remains usable and unchanged.

The runtime writes no durable readiness override. Its `Type=notify` unit stays activating until the
first complete check succeeds. It repeats every check every 30 seconds and sends `STOPPING=1` before
exiting on drift; #2140 must also call the same check immediately before enabling an adapter or
admitting its first request. Journal restoration is therefore a prerequisite: any
corrupt, foreign, missing, extra, longer, or valid-prefix-truncated lane fails the inventory/local
bijection or trusted-head comparison before readiness. Ansible additionally creates a transient
proof identity, certificate, and active incarnation credential; verifies server, client, and worker
authentication plus `provider-not-configured`; and removes the proof identity and its material
and retires its database incarnation before declaring provisioning complete.

## Threat model

### Boundary inventory

The added boundaries are systemd credential delivery into the authority process, the authority
database connection, the authority journal filesystem, a mutually authenticated local request
stream, and a distinct authority-owned libvirt session. No network listener or caller-selected
path, URI, command, provider definition, or provider credential is added. The existing fixed-worker
endpoint is unchanged.

### Actors and controls

- A compromised fixed worker or reconciler retains existing-provider access but must not reach the
  dormant authority endpoint's credentials, request or mutation socket, helper, journal, runtime,
  or provider objects. Distinct Unix identities/session namespaces, ownership, missing TLS material,
  and negative Ansible probes enforce this.
- A client-group member without an accepted certificate cannot finish the TLS handshake. A holder
  of an accepted proof certificate without a current incarnation credential cannot become
  `AuthenticatedPeer`. A fully authenticated client still cannot reach provider code while the
  dispatcher is dormant.
- A compromised tenant cannot select any host path or credential because the host commands accept
  configuration only from root-owned files and systemd credential descriptors.
- A local platform administrator remains trusted, matching ADR-0584. Root can bypass filesystem
  ACLs and is explicitly outside this boundary.
- A stale authority process cannot claim readiness from a cached stamp: systemd publishes ready
  only after startup reconstructs all evidence; every 30-second interval repeats it and retracts
  readiness before exit on drift.
- A malicious or replaced path component cannot redirect privileged writes: provisioning and the
  runtime reject symlinked or foreign-owned protected paths.

### Failure disclosure

Readiness errors name a bounded component and reason, never a path supplied by an untrusted caller,
credential content, DSN, provider output, or journal bytes. systemd restarts the service on
failure; Ansible stops provisioning when the one-shot probe or any negative access proof fails.

### Explicitly out of scope

This design does not protect against root, bind local- or remote-libvirt provider primitives,
advertise a provider capability, issue production worker credentials, or orchestrate external-boot
lifecycle state. Those are accepted exclusions or owned by #2140/#2118 and the existing worker
lifecycle authority.

## Verification

- Host-runtime tests exercise safe path/credential checks, role-shape failures, journal restore
  failures, bounded framing/diagnostics, mutual TLS, database-derived peer identity, and dormant
  dispatch refusal.
- Structural deployment tests prove account/session separation, installed modes, systemd
  supervision, credential delivery, and absence of worker/reconciler authority-endpoint access.
- The authorized Ubuntu 26.04 x86_64 carrier receives a Git bundle containing the exact local HEAD
  into a SHA-named proof directory. The proof overrides both `live_vm_repo_url` with that bundle
  and `live_vm_repo_version` with the full SHA, then asserts the installed revision equals that
  SHA. It bootstraps a local
  peer-authenticated PostgreSQL database and `kdive-provider-authority` LOGIN, generates a
  proof-only CA/server/client chain and active worker-incarnation credential, executes the role from
  a clean KDIVE state, starts/restarts both endpoints, proves server and client authentication,
  proves valid requests stop at `provider-not-configured`, runs negative readiness/access probes,
  injects drift, and verifies readiness retracts. The role creates the real `kdive` reconciler
  service identity before its denial probe. Native provider behavior remains #2151/#2152 work.
- Provider-neutral adversarial tests explicitly cover unresolved calls across takeover/restart,
  valid-prefix journal loss, independent lane heads, and stale provider/core writes.
- Existing composition tests continue to prove capability advertisement is disabled.
- Focused tests, `just lint`, whole-tree `just type`, relevant hooks, and `just ci` gate delivery.

## Rollback

Before reverting, operators run the explicit idempotent `authority_host_teardown.yml` play. It
stops, disables, and removes both dormant authority units, request socket, and endpoint artifacts,
reloads systemd, revokes LOGIN, and proves the existing fixed-worker provider path still works. The
clean-host proof
executes deployment and this teardown. Migration 0125 is forward-only and remains applied but
inert; removing its function or grant would require an authorized later migration. Because provider
capability advertisement remains disabled, rollback has no active external-boot operation to
migrate. Journal and credential directories are retained rather than deleted; an operator may
inspect or remove them only after confirming no later #2140 deployment uses them.
