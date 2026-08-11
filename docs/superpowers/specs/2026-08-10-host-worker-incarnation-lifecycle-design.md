# Host worker incarnation lifecycle design

Issue: [#1926](https://github.com/randomparity/kdive/issues/1926)
Decision: [ADR-0555](../../adr/0555-systemd-supervises-host-worker-incarnations.md)
Existing boundaries: [ADR-0533](../../adr/0533-role-separated-worker-fence-evidence.md),
[ADR-0536](../../adr/0536-enforce-worker-fence-authority-boundaries.md)
Branch: `feat/host-worker-lifecycle-1926`
Base: `main`
Guardrails: focused pytest during TDD; `just ci` before implementation and review commits
ADR index coupling: not coupled; `docs/adr/` is the index.

## Frozen authority

- Scope identity: `https://github.com/randomparity/kdive/issues/1926` plus token
  `47272e08-1a3c-4792-b5f8-15d71b49d613`.
- Interaction: interactive.
- Outcome: restore the supported non-root host-process live-stack topology through a
  systemd-backed lifecycle authority so local workers start with registered incarnation
  credentials and both live-VM CI jobs reach their tests; reject root-worker launch with the
  supported remedy.
- Criteria: run each configured worker as a dedicated non-root systemd service with a unique
  registered 256-bit credential; deliver it through systemd credentials; use role-specific
  database authorities; retain exact unit/generation/cgroup termination evidence before cleanup;
  preserve ADR-0533 for one and several workers; reject root-worker mode before process launch;
  gate omissions before merge; expose redacted daemon logs when either live job fails.
- Provenance: issue #1926; accepted ADR-0533 and ADR-0536; user decisions on 2026-08-10 to adopt the
  non-root/session-only rescope and then the recommended systemd-backed rescope.
- Exclusions: root-worker mode as supported; a custom pidfd/guardian/socket process manager; a new
  privileged provider helper; optional or shared incarnation credentials; weakened artifact
  fences; unrelated provider behavior; unsupported deployment topologies.
- Surface: systemd-backed non-root worker lifecycle authority; live-stack launcher and root-mode
  gate; systemd units and host provisioning; explicit shared session-libvirt wiring; role bootstrap
  and environment wiring; incarnation seams; focused tests; live workflow diagnostics; required ADR
  and operator documentation; direct dependencies of those files.
- Ambiguities: none.

## Approaches

### Selected: systemd system units as durable runtime objects

Use one installed system worker unit per slot and one lifecycle-witness service. Systemd owns the
worker process tree, credential copy, cgroup, and retained terminal unit state. The witness owns
database registration and evidence ordering. Its restart reconciles the systemd objects rather than
reconstructing lost PID relationships.

This uses the repository's existing systemd deployment surface and accepted root deployment trust.
It adds no database migration or general privileged provider interface.

### Rejected: custom guardian and pidfd broker

That design must implement clean privilege transitions, parent/reaper ownership, pre-auth socket
admission, PID reuse protection, crash adoption, and cross-UID handoff. Systemd already owns those
process-manager responsibilities and retains the runtime object after the witness restarts.

### Rejected: invoking-user or root worker

The CI/operator account can administer Docker or sudo, and root can inspect every co-resident
process. A worker under either identity can acquire witness authority. The dedicated worker account
has neither capability.

## Architecture

### Provisioned identities and libvirt

Host provisioning creates bounded no-login `kdive-worker-1` through `kdive-worker-8` accounts and
separate no-login `kdive-server`, `kdive-reconciler`, and `kdive-libvirt` accounts. None has sudo or
Docker access. The checkout, installed lifecycle code, unit files, other roles' environment files,
and witness state are read-only or inaccessible to them. The system manager and lifecycle witness
are root deployment authority. A sudo-capable operator or hosted-runner setup performs provisioning;
the no-sudo self-hosted runner receives only the bounded lifecycle control socket. Both invoking
accounts perform short-lived control and observation only; no long-running KDIVE application
inherits their Docker or host authority.

`kdive-libvirt` owns one persistent session-libvirt daemon. Its socket uses one explicit
`qemu+unix:///session?socket=...` URI and a bounded group containing the configured slot accounts,
reconciler, and trusted live-test account. These consumers share socket access but never a login
UID. The worker, reconciler, reaper, mint, preflight, and live suite all receive that same URI.
Staged images and provider runtime directories use declared setgid ownership so staging, workers,
reconciler, and tests have only their required access.

Hosted live CI provisions the accounts, socket, directories, and units before bring-up. The
self-hosted `live_vm_host` Ansible role declares the same state. Preflight verifies account IDs,
memberships, directory and unit ownership/modes, absence of worker sudo/Docker authority, and
libvirt/path access from every consumer identity. It also compares a digest of the privileged
lifecycle files in the checkout with the root-owned installed lifecycle manifest. A mismatch fails
before live testing and directs the operator to re-run the Ansible role; current application code
is never imported into the root service.

The Ansible role installs `kdive-live-worker-lifecycle.socket` as the runner's narrow root entry
point. Its fixed Unix socket is `root:kdive-live-control` mode `0660`; only the configured
`github-runner` account joins that group. The root lifecycle service verifies `SO_PEERCRED` and
accepts one versioned Unix seqpacket of at most 4 KiB containing only `start`, `status`, `stop`, or
`report`, an ASCII GitHub run identifier of at most 128 bytes, the worker count in `1..8`, and the
privileged-lifecycle digest. The byte ceiling applies per request; an oversized or malformed packet
is rejected without state change and the client may retry a corrected request. It accepts no
command, unit name, environment value, credential, or caller-selected filesystem path. The service
derives the single Ansible-configured workspace and unit set, serializes one active run, and rejects
a mismatched run identifier. `start` and `stop` return an operation state for `status` polling rather
than holding the control connection across daemon work. Force cleanup remains root-operator-only
and is not exposed on the socket.

The lifecycle service uses only its provisioned root-owned code. After the digest check it may pass
the configured checkout's `src` directory to server, reconciler, and worker units, which execute it
only under their dedicated non-root identities. It validates that the workspace and source tree are
owned by the runner, are not group/world writable, contain no symlink in the resolved path, and are
readable but not writable by application identities. It stages no caller-controlled executable as
root. The hosted ephemeral job installs the same socket, service, identities, and checks during its
root-capable setup; the self-hosted job only invokes the bounded client.

### Systemd unit topology

Add the fixed system template `kdive-live-worker@.service`. Instance names are decimal slots in
`1..KDIVE_WORKER_COUNT`, whose existing maximum is eight. Each unit has:

- `User=kdive-worker-%i`, its private primary group, and the bounded libvirt/runtime supplementary
  group;
- a fixed `ExecStart` for the configured KDIVE Python and `-m kdive worker`;
- `Restart=no`, so the witness is the only generation creator;
- `StartLimitIntervalSec=0`, so witness retries of the same registered generation cannot be rejected
  by systemd's service start limiter;
- `KillMode=control-group`, so stop reaches the entire incarnation process tree;
- `ExitType=cgroup`, so completion follows the complete incarnation cgroup rather than only the main
  process;
- `RemainAfterExit=yes`, so an empty cgroup leaves a retained terminal unit;
- `LoadCredential=kdive-worker-incarnation:<root-only per-generation source>`;
- one root-owned role environment file containing only worker-role settings, plus a separate fixed
  per-slot identity environment reference owned by the witness, and no witness-role environment
  file; and
- a distinct health bind and journal identity per slot.

Add `kdive-live-worker-lifecycle.service` with `Restart=on-failure`. It runs the fixed lifecycle
entrypoint as root, receives only the lifecycle-witness DSN, owns the root-only state and credential
source directories, and may inspect/start/signal/stop/reset only the fixed worker template
instances. Root is already the accepted host deployment authority; no worker process receives its
credentials or control surface.

The live topology also installs server and reconciler system units under their dedicated accounts.
Their root-owned environment files contain only the matching role DSN and required non-database
settings. The reconciler joins the libvirt socket group; neither role can read witness or worker
credentials, control worker units, invoke sudo, or reach Docker.

User-systemd deployment units remain unchanged and are not a worker-fence authority. The live-stack
launcher refuses to fall back to them or to direct `python -m kdive worker`.

### Incarnation state machine

For each slot, the witness uses one root-owned generation bundle under a fixed slot directory. The
bundle contains a bounded state document, the credential source, and an identity environment file.
The state contains schema version, unit name, generation, holder, credential hash, phase, host boot
identifier, optional systemd invocation identifier, and service result. The identity file contains
only the fixed unit name, random lowercase-hex generation, and derived holder. The credential is not
an environment value. All opens use fixed directories, no symlink following, and bounded names.

To publish `prepared`, the witness creates a mode-`0700` temporary generation directory under the
fixed parent, writes the credential, identity, and state with their final modes, `fsync`s every file
and the temporary directory, then atomically renames that directory to the absent fixed slot name
and `fsync`s the parent. Registration begins only after that rename. A crash before the rename
leaves an unregistered temporary directory that bounded startup enumeration removes after verifying
its shape; a crash after the rename leaves one complete enumerable `prepared` bundle. Later phase
writes use atomic replacement of `state.json` plus directory `fsync`. Cleanup after evidence
atomically renames the bundle to a bounded tombstone, `fsync`s the parent, unlinks only its three
known regular files, and removes the directory.

The fixed unit references the slot bundle's identity and credential paths. Before every start or
re-adoption, the witness parses the identity file itself and requires its unit, generation, and
holder to equal the state and registered binding. The host authority deliberately uses the existing
database authority kind `local`; its binding is the fixed unit plus generation, so no new SQL kind
or migration is required. The phases are:

1. `prepared`: mint generation and credential, then publish the complete generation bundle;
2. `registered`: idempotently register those exact durable facts through the witness database role,
   then persist and `fsync` the phase after the transaction commits;
3. `starting`: prove the identity file matches, and the unit is inactive with no pending job or
   invocation, then persist and `fsync` start intent with the current host boot identifier before
   asking systemd to start the fixed unit;
4. `started`: adopt and wait for systemd's exact pending start job, then persist its invocation
   identifier and the phase when activation begins;
5. `terminal`: observe the matching retained unit with an empty cgroup; and
6. `evidenced`: commit terminal evidence, stop/reset the retained unit, then remove the generation
   bundle.

The holder is derived from the bounded unit name and random generation, not a reusable PID. The
authority binding stores those same values. The worker receives both through its allowlisted systemd
environment and must derive the identical holder before credential authentication.

Registration failure never starts the unit. An ambiguous database result retains the `prepared`
state and credential for an exact retry; the existing database function accepts the replay when all
facts match an active row. A definitive rejection may remove the unused source only after proving
that no row exists. A restart from `registered` durably advances to `starting` and starts the same
generation. In `starting`, the unit's pending start job proves that systemd accepted but has not yet
activated the request, so the witness adopts and waits for that job. A non-empty invocation
identifier is re-adopted after activation begins. If the host boot identifier still matches and the
unit remains inactive with neither a pending job nor an invocation identifier, no runtime
invocation exists and the witness safely retries the same generation. A pending non-start job,
changed boot identifier, missing unit, or contradictory state fails closed. Start failure after
systemd accepted the invocation is a terminal incarnation only when the retained failed unit,
matching generation, and recorded invocation identifier agree; the witness then records `failed`
before cleanup. A live unit is never replaced in place or reset before evidence.

For graceful stop the witness sends SIGTERM to the unit cgroup without unloading it, waits for the
exact cgroup to empty, then records evidence. A bounded stop timeout leaves the unit and fence
retained; the operator retries or uses the explicit force path, which may strand the fence but never
creates evidence.

A clean worker exit is retained as `active (exited)` and an abnormal exit is retained as `failed`.
The witness accepts either terminal state only when the exact unit, generation, invocation
identifier, and empty cgroup match. It maps systemd `success` to `succeeded`, `exit-code` to
`failed`, `resources` with a matching invocation to `failed`, and signal, core-dump, timeout,
watchdog, and OOM-kill results to `killed`. A same-boot `resources` failure with no pending job or
invocation created no runtime, so the witness resets only the failed unit state and retries the same
registered generation. Provisioning disables the unit start limiter; `start-limit-hit` therefore
proves unit drift and fails closed, as does any other unknown state or result.

On witness restart, reconciliation runs before new starts. It enumerates fixed configured units,
published slot bundles, pre-publication temporary directories, and cleanup tombstones with hard
ceilings. It rejects unknown entries and validates regular-file types, ownership, modes, and bundle
agreement before acting. `prepared` replays registration with the same facts;
this covers a crash immediately before or after the database commit. `registered` safely advances
to `starting` and starts the same generation. On the same host boot, `starting` adopts a pending
start job, retries an inactive unit with no job or invocation identifier, or re-adopts its existing
invocation. Live matching cgroups are re-adopted. Empty matching units receive evidence and cleanup.
A pending non-start job, changed boot identifier, missing state, a unit missing from `starting`
onward, generation or invocation mismatch, duplicate state, unexpected instance, or ambiguous
cgroup status fails closed and names the slot. Systemd and database outages leave all objects for
retry.

### Worker credential and identity input

Add a systemd worker credential transport selected only when the unit and generation settings are
both present. The worker reads `kdive-worker-incarnation` from `$CREDENTIALS_DIRECTORY`, bounded to
the exact 64 lowercase-hex characters plus one overflow byte. Missing, malformed, oversized, or
unsafe input fails without falling back to the Compose/Kubernetes file.

The worker registers the credential with its redaction registry before database authentication.
Compose and Kubernetes retain their existing fixed private file handoff and identity derivation.
One authority transport remains mandatory.

### Host launcher and role wiring

`scripts/live-stack/env.sh` defines host-reachable non-secret settings and local role member names.
The root lifecycle service reads fixed local development passwords or operator-supplied role DSNs
from its provisioned configuration, not from the control request. The shared development DSN
remains available only to explicit short-lived helpers. No long-running process receives it.

Bring-up ordering is:

1. reject root-worker mode and an absent/unsafe systemd boundary;
2. start backends and wait for Postgres;
3. send `start`; the root service applies migrations with the migration-owner DSN;
4. the root service runs idempotent local runtime-role bootstrap;
5. it writes separate root-owned role environment files;
6. it starts server and reconciler under their dedicated accounts with their respective DSNs;
7. it registers and starts every configured worker slot through the lifecycle witness;
8. settle on server, reconciler, witness, and the exact worker count; and
9. reconcile inventory only after all real workers remain authenticated and alive.

External role provisioning remains supported: `KDIVE_LOCAL_ROLE_BOOTSTRAP=0` requires role DSNs in
root-owned provisioned configuration. The lifecycle service scrubs unrelated role variables.
Graceful restart/down sends `stop`; the witness terminates and evidences every worker before the
runner tears down backends. It refuses completion while a unit, bundle, or incarnation remains
unresolved.

### Failure diagnostics in live CI

Add one bounded redacting reporter owned by the root lifecycle service and used by both live jobs
through the fixed `report` request. It reads only the lifecycle and configured application units,
and returns regular files under `.live-stack-logs`; the caller cannot supply a unit or source path.
Before reading logs, it seeds literal-value redaction from every published worker credential and
every role-secret value in the root-owned environment files, then adds URL-userinfo and
secret-named key/value rules. A malformed or over-limit secret source withholds that log source
rather than emitting unredacted content.

For each source the reporter captures at most 320 KiB into a bounded buffer, applies literal and
structural redaction to the complete buffer, and only then truncates emitted content to 256 KiB.
Registered literal values are bounded to 4 KiB; the input allowance covers a value crossing the
output cutoff. The reporter emits at most 1 MiB total in fixed source order, never follows symlinks,
escapes hostile journal control characters, and succeeds with an explicit marker when no safe log
source exists. It writes outputs with create-new/no-follow semantics and never returns its redaction
seed set.

Each live job adds a final `if: failure()` step invoking the bounded control client and reporter.
Startup, role, lifecycle, and test failures therefore retain their daemon exception without
replacing the original failed step.

## Failure contracts

- Root-worker or direct-worker request: fail before application launch and name the provisioned
  systemd/non-root remedy.
- Unsafe or absent systemd boundary: fail before opening role credentials and name the provisioning
  mismatch.
- Privileged lifecycle digest mismatch or unauthorized control peer: perform no root action and name
  the Ansible reprovisioning or identity remedy.
- Missing witness or worker DSN: no worker starts; the affected role and environment file are named.
- State, credential, or environment path ownership/mode mismatch: fail closed without following or
  replacing the path.
- Registration failure: no systemd start; ambiguous outcomes retain the exact handoff for replay,
  and only a proven absent row permits discard.
- Start failure after registration: retain the unit, record `failed`, then clean the handoff.
- Worker credential or identity mismatch: worker exits; retained unit becomes failure evidence.
- Worker crash: the failed unit, generation, and invocation remain; witness records mapped evidence
  before reset or replacement.
- Pre-invocation resource failure: reset only the failed unit state and retry the same registered
  generation on the same boot; retain the credential and fence.
- Start-limit result: fail closed as provisioned-unit drift; do not reset or mint a generation.
- Witness crash: systemd restarts it; workers and retained unit state survive for reconciliation.
- Stop timeout or database outage: unit, state, credential source, and fence remain for retry.
- Multi-worker bind collision: the exact instance fails and blocks bring-up without affecting other
  generations.
- System manager restart: installed units and root state remain; witness reconciles before starts.
- Force cleanup or host loss: may strand a fence, but cannot fabricate evidence or release it.
- Missing logs: reporter states that no source was available and preserves the earlier failure.
- Unsafe or oversized redaction seed: withhold the affected source and preserve the earlier failure.

## Threat model

Untrusted actors are a compromised worker, server, or reconciler; malformed systemd
environment/credential/state input; another unprivileged local process; stale database state; and
hostile log content. Root, the system manager, migration owner, and lifecycle-witness role are
trusted deployment authorities. The trusted workflow/test account provisions and observes the
stack but does not run long-lived KDIVE application code under its own identity.

Added or widened boundaries are:

- **System manager → worker.** Fixed unit template, one dedicated UID per slot, no sudo/Docker
  access, read-only code/configuration, allowlisted environment, per-UID credentials, and cgroup
  ownership isolate each worker from lifecycle authority and sibling credentials.
- **System manager → server/reconciler.** Dedicated non-escalating accounts, role-specific
  environment files, and fixed units keep compromised application roles away from witness and
  operator authority.
- **Runner → lifecycle witness.** A fixed peer-credentialed socket, bounded verb schema, installed
  lifecycle digest, derived workspace/unit paths, and one-run serialization expose lifecycle
  control without general sudo, arbitrary unit control, or root execution of checkout code.
- **Witness → witness database.** Existing bounded registration/termination functions receive the
  exact unit/generation binding and hash. The worker DSN is absent.
- **Systemd credentials → worker.** Root-only unique source, systemd copy, exact-size/hex
  validation, redaction-before-authentication, and cleanup after evidence bound the handoff.
- **Worker unit/cgroup → termination evidence.** Matching root state, retained named unit, empty
  exact cgroup, and evidence-before-reset prevent PID absence or timeout from becoming proof.
- **Worker session libvirt → live tests.** A separate daemon identity, one explicit socket URI,
  bounded group membership, setgid paths, and per-consumer preflight prevent caller-relative
  session skew without merging worker credentials under one UID.
- **Live CI → system journal.** Root-side literal seeding from active secrets, structural redaction
  before truncation, fixed sources, per-source and total byte bounds, and regular-file checks
  constrain failure disclosure even when a compromised application writes bare secrets.

Explicitly out of scope:

- Root-worker mode and root-only provider operations; they require another privileged-interface
  decision.
- User-systemd units as fence authority.
- A malicious root, system manager, migration owner, or trusted workflow account.
- Host power loss or explicit force cleanup; these may strand pins but cannot release them.
- Compose and Kubernetes lifecycle implementations, which remain unchanged.

## Executable acceptance proofs

1. Worker tests redden for missing/malformed systemd identity, credential, generation, overflow,
   cross-transport fallback, and credential/holder mismatch.
2. Lifecycle unit tests prove register-before-start, unique slot UIDs, generations, and credentials,
   sibling credential denial, identity-bundle agreement, `local` authority mapping,
   evidence-before-reset/cleanup, cgroup-empty checks, outcome mapping, and retry retention.
3. Reconciliation tests inject a crash before and after every durable phase boundary, including
   before registration, after commit but before the phase write, immediately before the systemd
   request, after acceptance while the start job is queued, after activation, and before persisting
   the invocation identifier. They prove pending-start-job adoption, no-job/no-invocation same-boot
   retry, accepted-invocation adoption, non-start-job and boot-change refusal, clean and non-zero
   exits, fatal signals, timeouts, watchdog failures, OOM kills, resource failures before and after
   invocation, start-limit drift, unknown-result refusal, missing/mismatched/duplicate state, system
   manager/database outages, live adoption, empty-unit evidence, pre-publication orphan and cleanup
   tombstone recovery, and force behavior. Publication tests inject crashes after every file write,
   file `fsync`, directory `fsync`, bundle rename, parent `fsync`, and registration commit.
4. Unit-shape and provisioning tests pin fixed commands, distinct slot/server/reconciler/libvirt
   identities, absence of sudo and Docker authority, role-file separation, `Restart=no`, disabled
   start limiting, `KillMode=control-group`, `ExitType=cgroup`, `RemainAfterExit=yes`, credential
   loading, path modes, and the explicit libvirt socket contract. A real unit proof leaves a child
   alive after the main process exits and verifies that evidence waits for the exact cgroup to empty.
5. A disposable-Postgres process test starts one and then several real workers through the lifecycle
   seam, observes distinct active incarnations and worker-role connections, terminates them, and
   observes exact evidence. A systemd-hosted live proof exercises the real units.
6. Script and control-protocol tests prove root/direct mode rejection, exact peer admission, schema
   and size bounds, fixed workspace/unit derivation, lifecycle-digest mismatch refusal,
   migration/bootstrap/role ordering, exact worker count, one-run serialization, restart refusal on
   unresolved evidence, and no shared DSN reaches a daemon. An Ansible-hosted proof invokes every
   allowed operation as `github-runner`, rejects another UID, and proves the account still has no
   general sudo or arbitrary systemd control.
7. Workflow/reporter tests prove both live jobs invoke failure-only diagnostics and redact bare
   worker credentials and role passwords, including values crossing the output cutoff. They cover
   hostile journal formatting, multiple sources, per-source and total bounds, malformed seed
   withholding, no symlink following, and rejection of arbitrary units.
8. Hosted setup and self-hosted Ansible provisioning pass every worker, reconciler, and test
   identity's libvirt preflight against the same daemon before their first real live proof; an
   actual two-worker run proves one slot cannot read the other's credential.
9. Focused Python, shell, systemd-unit, Ansible, configuration-doc, and workflow checks pass,
   followed by `just ci` with no warnings.

## Rollback

The change adds no schema migration. Before reverting, ask the witness to stop every worker and
confirm all incarnation rows are terminal and every credential source is gone. Reverting units and
scripts restores the old direct launcher, but that launcher remains incompatible with mandatory
credentials and is not a supported worker path. Compose and Kubernetes deployments are unchanged.
