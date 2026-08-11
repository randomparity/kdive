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
`report`, an ASCII control-run token of at most 128 bytes, the worker count in `1..8`, and the
privileged-lifecycle digest. The byte ceiling applies per request; an oversized or malformed packet
is rejected without state change and the client may retry a corrected request. It accepts no
command, unit name, environment value, credential, or caller-selected filesystem path. The service
derives the single Ansible-configured workspace and unit set, serializes one active run, and rejects
a mismatched control-run token. `start` and `stop` return an operation state for `status` polling rather
than holding the control connection across daemon work. Force cleanup remains root-operator-only
and is not exposed on the socket.

CI derives the token from the numeric repository ID, workflow run ID, workflow run attempt, and
fixed job name, so a GitHub rerun is a new owner while every step in one job derives the same value.
Local `up.sh` mints a random 256-bit lowercase-hex token and atomically persists it in the fixed
runner-owned mode-`0600` control-state file; separate-shell `status`, `report`, and `down` read that
file. An active local token is resumed rather than replaced. After completed `down`, the client
removes its token file. A repeated `start` for a completed token returns the retained completed
result and never creates another run; only a different token may retire that completed record and
publish a new owner. Delayed duplicate requests therefore cannot become new attempts.

Before any migration, role-file write, or unit transition, an accepted `start` atomically publishes
one root-owned mode-`0600` run record containing schema version, control-run token, lifecycle digest,
worker count, workspace identity, journal start cursor, per-run unit invocation identifiers, and
operation phase (`starting`, `running`, `stopping`, or
`complete`) plus a nullable run outcome and report state (`not-required`, `pending`, or `complete`).
A fixed receipts directory contains exactly one mode-`0600` receipt for each configured slot. Each
receipt retains the control-run token, slot, unit, nullable generation/incarnation/binding/hash until
publication, lifecycle phase, terminal outcome, database termination-commit flag, and bundle-cleanup
flag. Its terminal disposition is exactly one of `not-created`,
`discarded-before-registration`, or `cleaned`. The service builds the record and initial
`pending` receipts in a temporary run directory, `fsync`s every file and directory, atomically
renames it to the absent active-run path, and `fsync`s the parent before taking another action.
Receipt and run-record updates use atomic replacement plus directory `fsync`. That complete run
bundle is the serialization and positive-recovery authority across service restarts. Every
generation bundle includes the same control-run token.

Every atomic file replacement uses one exact sibling name: `.run.json.next`,
`.slot-<n>.json.next`, or `.state.json.next`. It is opened create-exclusive, no-follow, with the
canonical file already present. On restart, a valid canonical file wins; the service verifies that
its optional `.next` sibling is one bounded root-owned regular file, unlinks it, and `fsync`s the
directory before reconciliation. A missing canonical, multiple candidate entries, wrong type,
owner, mode, or size fails closed. Initial run and generation publication use atomic directory
rename instead, so no valid recovery path needs to promote a replacement temporary.

On restart the service loads and validates the run record and fixed receipt set before units or
requests, reconciles only matching bundles and units, and resumes the recorded operation. A repeated
request with identical facts is idempotent; a different control-run token or changed immutable fact
fails closed while the record exists. `running` requires every receipt to describe a live matching
`started` invocation. Any terminal slot observed during `starting` or `running`, before a durably
requested stop, first persists run outcome `failed` and report state `pending`. The witness stages a
safe failure report while every retained unit, role file, and credential source still exists, then
moves to `stopping`, evidences that slot, and terminates and evidences the remaining slots. A caller
stop is serialized under the same run lock. While every configured slot and seed is still present,
it advances `not-required` to `pending`, stages the bounded sanitized baseline, and enters
`stopping` only with report `complete`; an existing `failed` or `pending` state is preserved and
completed first. It then signals units, so subsequent expected terminations do not take the failure
path. A later stop failure advances a null outcome to `failed` without downgrading the report. If all
seeds still exist, the helper may atomically replace the baseline; after any seed deletion it keeps
the baseline and appends only non-secret operation receipts plus the fixed property-only snapshots
for exact stop/helper invocations, never rereading application journals. Outcome may advance only
from null to `failed`; report state may advance only `not-required` to `pending` to `complete`, and
retries preserve `complete`. `complete` requires every expected receipt to be in one positive
terminal disposition plus the server, reconciler, reconcile-once invocation receipt/reset, role
files, and report staging to have their specified terminal disposition.

The fixed `report` operation ensures the report oneshot atomically publishes sanitized output
beneath a root-owned staging directory derived from the active control-run token; it does not return
journal bytes in the 4 KiB control response. The response names only fixed staged files that the
authorized peer may read, and the client copies them into its checkout. A retry for the same run
adopts a complete report or replaces an incomplete temporary report. Report staging always precedes
destructive `stop` work. The run record
reaches `complete` only after every slot is evidenced and cleaned, server and reconciler are stopped,
and the cleanup oneshot's non-secret receipt proves the role files removed. The completed record and
report remain as an idempotent `stop` result until the next `start` atomically retires them before
publishing the new owner. A crash at any boundary reconstructs the same owner and cannot let another
run take over unresolved state.

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
source directories, and uses a literal allowlist to inspect or operate only
`kdive-live-stack-prepare.service`, `kdive-live-stack-report.service`,
`kdive-live-stack-cleanup.service`, `kdive-live-server.service`,
`kdive-live-reconciler.service`, `kdive-live-reconcile-once.service`, and configured
`kdive-live-worker@<slot>.service` instances in `1..8`. It accepts no unit name through the control
protocol. Root is already the accepted host deployment authority; no worker process receives its
credentials or control surface.

Three fixed root oneshots keep other database and secret authorities out of that long-lived service.
`kdive-live-stack-prepare.service` reads provisioned migration and role configuration, applies
migrations, bootstraps roles, writes the fixed role environment files, writes a non-secret completion
receipt, and exits before any application unit starts. `kdive-live-stack-report.service` reads the
fixed role files, active generation credentials, and fixed journals; publishes the bounded sanitized
report and a non-secret manifest; and exits immediately. `kdive-live-stack-cleanup.service` verifies
and unlinks only the fixed role files, `fsync`s their directory, writes a non-secret cleanup receipt,
and exits without database configuration. All three use installed root-owned code and derived paths,
accept no socket or caller arguments, and are invoked and observed by the lifecycle service only
through their fixed unit names and non-secret receipts. The lifecycle service's configuration and
environment never contain migration-owner, server, worker, or reconciler credentials, and its
sandbox makes the role environment directory unreadable.

The live topology also installs `kdive-live-server.service` and
`kdive-live-reconciler.service` under their dedicated accounts with `Restart=no`. A fixed
`kdive-live-reconcile-once.service` runs as `kdive-reconciler`, receives only the reconciler
environment file, executes the installed `reconcile-systems` command once, and uses
`RemainAfterExit=yes` so its exact invocation identifier and success result remain observable. The
lifecycle service starts it only after all workers are authenticated, persists its invocation and
result in the run record, and treats any non-success result as a failed run. After that positive
receipt is durable, it stops/resets the retained oneshot, verifies inactive state with no pending
job, and persists `reconcile_once_reset=true` before the run may become ready or complete. On
restart, a retained unit without a receipt is harvested first; a receipt without the reset flag
resumes reset; an inactive unit plus the exact receipt permits the flag update. The next run cannot
retire the completed record or start reconcile-once until this transition finishes. Neither the
caller nor the lifecycle service receives its database DSN.
Their root-owned environment files contain only the matching role DSN and required non-database
settings. The reconciler joins the libvirt socket group; neither role can read witness or worker
credentials, control worker units, invoke sudo, or reach Docker.

User-systemd deployment units remain unchanged and are not a worker-fence authority. The live-stack
launcher refuses to fall back to them or to direct `python -m kdive worker`.

### Incarnation state machine

For each slot, the witness uses one root-owned generation bundle under a fixed slot directory. The
bundle contains a bounded state document, the credential source, and an identity environment file.
The state contains schema version, control-run token, unit name, generation, holder, credential
hash, phase, host boot identifier, optional systemd invocation identifier, and service result. The
identity file contains only the fixed unit name, random lowercase-hex generation, and derived
holder. The credential is not an environment value. All opens use fixed directories, no symlink
following, and bounded names.

To publish `prepared`, the witness creates a mode-`0700` temporary generation directory under the
fixed parent, writes the credential, identity, and state with their final modes, `fsync`s every file
and the temporary directory, then atomically renames that directory to the absent fixed slot name
and `fsync`s the parent. Registration begins only after that rename. A crash before the rename
leaves an unregistered temporary directory that bounded startup enumeration removes after verifying
its shape; a crash after the rename leaves one complete enumerable `prepared` bundle. Later phase
writes use atomic replacement of `state.json` plus directory `fsync`. Cleanup after evidence
atomically renames the bundle to a bounded tombstone, `fsync`s the parent, unlinks only its three
known regular files, and removes the directory.

The slot receipt begins as `pending`. After generation-bundle publication, the witness persists its
exact generation and `prepared` facts; until cleanup, a lagging receipt may be advanced only from a
validated matching bundle, unit, and database row. After the termination transaction commits, the
witness persists `evidenced` in the bundle, then persists an `evidenced` receipt with the exact
generation, binding, outcome, and termination-commit flag and verifies the matching terminal
database row. Only that positive receipt permits unit reset and bundle deletion. After deletion it
persists `cleaned`. A missing bundle with a published generation and an earlier receipt phase fails
closed; a missing bundle with an `evidenced` or `cleaned` receipt is accepted only after the same
exact database verification and expected inactive unit state. `not-created` and
`discarded-before-registration` follow the separate no-incarnation rules below. Absence by itself is
never evidence.

When a run durably enters `stopping`, no slot-creation step may begin or publish afterward. For a
`pending` receipt with null generation, the witness first resolves any validated pre-publication
temporary, then requires no published bundle, pending systemd job, invocation identifier, or active
cgroup before atomically persisting `not-created`. This is positive creation-state accounting, not
incarnation termination evidence. If a generation bundle was published but registration is proven
absent, the witness requires the same no-runtime facts, persists
`discarded-before-registration` with the exact generation and hash, and only then removes the unused
bundle. An ambiguous registration result replays the exact registration facts; it never takes the
discard path. A registered incarnation can reach only `cleaned` through exact terminal evidence.
Run completion accepts the three terminal dispositions according to those mutually exclusive facts.

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
6. `evidenced`: commit terminal evidence, persist the positive slot receipt, stop/reset the retained
   unit, remove the generation bundle, and persist `cleaned` in the receipt.

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
the fixed run receipt set, published slot bundles, pre-publication temporary directories, and cleanup
tombstones with hard ceilings. It rejects unknown entries and validates regular-file types,
ownership, modes, run/receipt/bundle agreement, and positive terminal database receipts before
acting. `prepared` replays registration with the same facts;
this covers a crash immediately before or after the database commit. `registered` safely advances
to `starting` and starts the same generation. On the same host boot, `starting` adopts a pending
start job, retries an inactive unit with no job or invocation identifier, or re-adopts its existing
invocation. Live matching cgroups are re-adopted. If reconciliation observes an empty matching unit
while the run is `starting` or `running`, it persists the failed-run/report-pending transition and
completes safe report staging before terminal evidence or cleanup. Only a run already durably in
`stopping` may send an empty matching unit directly through evidence and cleanup. A pending
non-start job, changed boot identifier, missing pre-evidence state, a unit missing from `starting`
onward without an exact positive receipt, generation or invocation mismatch, duplicate state,
unexpected instance, or ambiguous cgroup status fails closed and names the slot. Systemd and
database outages leave all objects for retry.

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
The fixed preparation oneshot reads local development passwords or operator-supplied role DSNs from
root-owned provisioned configuration, not from the control request. The shared development DSN
remains available only to that short-lived helper. No long-running process receives it.

Bring-up ordering is:

1. reject root-worker mode and an absent/unsafe systemd boundary;
2. start backends and wait for Postgres;
3. send `start`; the lifecycle service starts and observes the fixed preparation oneshot;
4. the oneshot applies migrations and runs idempotent local runtime-role bootstrap;
5. the oneshot writes separate root-owned role environment files and exits;
6. the lifecycle service starts server and reconciler under their dedicated accounts;
7. it registers and starts every configured worker slot using only its witness DSN;
8. settle on server, reconciler, witness, and the exact worker count; and
9. start the fixed reconcile-once unit, retain its exact successful invocation receipt, and return
   only after that role-separated inventory pass completes.

External role provisioning remains supported: `KDIVE_LOCAL_ROLE_BOOTSTRAP=0` requires role DSNs in
root-owned provisioned configuration. The preparation oneshot scrubs unrelated role variables.
Graceful restart/down sends `stop`; the witness terminates and evidences every worker before the
runner tears down backends, stops the fixed application units, then invokes and verifies the fixed
cleanup oneshot. It refuses completion while a unit, bundle, incarnation, nonterminal receipt, or
cleanup receipt remains unresolved.

### Failure diagnostics in live CI

Add one bounded redacting reporter in the fixed root report oneshot and use it for both live jobs.
The long-lived lifecycle service invokes it automatically whenever a run outcome first becomes
`failed`, before resetting any unit or deleting any generation bundle or role file. Cleanup remains
blocked while report state is `pending`; a helper crash leaves every seed and journal source for an
idempotent retry. Only an atomically published safe report manifest advances report state to
`complete` and permits destructive cleanup. A successful run uses `not-required` and needs no
report prerequisite.

Before publishing the active run bundle, the lifecycle service obtains a journald cursor and its
current systemd invocation identifier and stores both in that bundle. After systemd accepts each
preparation, server, reconciler, worker, or reconcile-once activation, the service persists that
unit's exact `_SYSTEMD_INVOCATION_ID` before treating the step as ready or starting the next one. A
restarted lifecycle service appends its new invocation identifier before resuming run work. The
report helper accepts per-run unit journal entries only when their invocation identifier is in the
run record; lifecycle entries must be after the stored cursor and match a lifecycle invocation
recorded for that run. A source with no accepted invocation emits only an explicit
no-current-invocation marker and a property-only D-Bus/`systemctl show` snapshot allowlisting `Id`,
`LoadState`, `ActiveState`, `SubState`, `Result`, `InvocationID`, `Job`, `NRestarts`, `ExecMainCode`,
and `ExecMainStatus`. It never invokes `systemctl status`, requests journal lines, renders arbitrary
properties, or falls back to a fixed-unit tail.

The helper reads only the lifecycle and configured application units and returns regular staged
files through the later fixed `report` request; the caller cannot supply a unit or source path.
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

Each live job first adds an `if: failure()` staging step before its always-run `stop`. That operation
adopts the already staged automatic failure report or durably sets run outcome `failed` and report
state `pending` before starting the helper when failure happened outside a daemon transition. Stop
itself stages the healthy baseline before destructive work. A final `if: failure()` publication step
after stop adopts the retained report, including any stop-failure augmentation, and copies only its
manifest-listed files. The client prints fixed headings and bounded sanitized content into the
Actions job log, prefixing every content line with `| ` so hostile text cannot become a workflow
command. The 1 MiB report ceiling is also the per-job console-publication ceiling; reaching it emits
one truncation marker and `report` still succeeds. Startup, role, lifecycle, test, and teardown
failures therefore expose their daemon exception without replacing an earlier failed step.
Self-hosted and hosted workspaces may then disappear without being the only copy visible to the
operator.

## Failure contracts

- Root-worker or direct-worker request: fail before application launch and name the provisioned
  systemd/non-root remedy.
- Unsafe or absent systemd boundary: fail before opening role credentials and name the provisioning
  mismatch.
- Privileged lifecycle digest mismatch or unauthorized control peer: perform no root action and name
  the Ansible reprovisioning or identity remedy.
- Mismatched control-run token or immutable run facts: leave the active run untouched and
  require the caller owning the recorded identifier to resume or stop it.
- Missing local control token: perform no lifecycle operation and direct the operator to the fixed
  control-state file or a new `up` after the prior run is confirmed complete.
- Missing witness or worker DSN: no worker starts; the affected role and environment file are named.
- Preparation oneshot failure: no application starts; its fixed non-secret receipt names the failed
  phase, the run stops slot creation and records every untouched receipt `not-created`, and role
  credentials leave memory when the helper exits.
- State, credential, or environment path ownership/mode mismatch: fail closed without following or
  replacing the path.
- Registration failure: no systemd start; ambiguous outcomes retain the exact handoff for replay,
  and only a proven absent row permits discard.
- Start failure after registration: retain the unit, record `failed`, then clean the handoff.
- Worker credential or identity mismatch: worker exits; retained unit becomes failure evidence.
- Worker clean exit or crash before requested stop: the retained unit, generation, invocation, role
  files, and all credential seeds remain while the witness persists run failure and stages the safe
  report; only then does it record mapped evidence before reset or replacement.
- Partial multi-worker start failure: persist run `failed`, stop/evidence every started slot, retain
  each positive slot receipt and every plaintext redaction seed until the safe report is staged, and
  report failure rather than replacing a failed slot in place.
- Pre-invocation resource failure: reset only the failed unit state and retry the same registered
  generation on the same boot; retain the credential and fence.
- Start-limit result: fail closed as provisioned-unit drift; do not reset or mint a generation.
- Witness crash: systemd restarts it; workers and retained unit state survive for reconciliation.
- Stop timeout or database outage: every unresolved unit, state, credential source, and fence remains
  for retry; already cleaned slots rely on positive receipts, and the pre-cleanup report plus
  property-only failure augmentation remains publishable.
- Multi-worker bind collision: the exact instance fails and blocks bring-up without affecting other
  generations.
- System manager restart: installed units and root state remain; witness reconciles before starts.
- Force cleanup or host loss: may strand a fence, but cannot fabricate evidence or release it.
- Missing logs: reporter states that no source was available and preserves the earlier failure.
- Unsafe or oversized redaction seed: withhold the affected source and preserve the earlier failure.
- Atomic-replacement temporary: retain the valid canonical record, remove its one validated `.next`
  sibling, and retry; any other shape fails closed with the exact path class.
- Cleanup oneshot failure: retain the run in `stopping`, retry the fixed helper, and do not claim
  `complete` until its non-secret receipt proves every fixed role file absent; retain and publish the
  pre-cleanup report with the helper's exact property-only failure augmentation.
- Stop during failed/pending report: preserve the monotonic failure state, retry safe report
  publication, and signal or clean no unit until the report manifest is durable.
- Missing or mismatched journal boundary: withhold that source, emit its fixed status snapshot and
  boundary error, and never fall back to history from the unit name.
- Reconcile-once failure: fail the run, stage the safe report, and retain its exact invocation result
  before stopping; no caller receives the reconciler DSN.

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
  lifecycle digest, attempt-unique durable token, derived workspace/unit paths, and one-run
  serialization expose lifecycle control without general sudo, arbitrary unit control, or root
  execution of checkout code.
- **Lifecycle witness → root oneshots.** Fixed unit names, installed code, derived inputs, non-secret
  receipts, immediate exit, and role-directory sandboxing keep migration/application credentials
  out of the long-lived socket service while preserving bounded preparation and reporting.
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
  before truncation, cursor plus invocation filtering, fixed sources, per-source and total byte
  bounds, and regular-file checks constrain failure disclosure even when a compromised application
  writes bare secrets. Line-prefixed console rendering exposes only sanitized current-run content and
  prevents hostile log lines from becoming Actions workflow commands.

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
   tombstone recovery, and force behavior. Early-failure tests prove preparation failure,
   pre-publication failure, and partial start converge through `not-created`,
   `discarded-before-registration`, or exact evidenced `cleaned` receipts without treating absence as
   termination. Running-phase clean and abnormal exit tests crash after terminal observation, run
   outcome/report-state persistence, safe report publication, evidence commit, receipt write, unit
   reset, and bundle deletion; witness restart must preserve that order. Publication tests inject
   crashes after every file write, file `fsync`, directory `fsync`, bundle rename, parent `fsync`,
   and registration commit. Every run-record, receipt, and generation-state replacement test covers
   write, file `fsync`, the fixed `.next` entry before rename, rename, parent `fsync`, validated
   cleanup, and malformed-entry refusal.
4. Unit-shape and provisioning tests pin fixed commands, distinct slot/server/reconciler/libvirt
   identities, absence of sudo and Docker authority, role-file separation, `Restart=no`, disabled
   start limiting, `KillMode=control-group`, `ExitType=cgroup`, `RemainAfterExit=yes`, credential
   loading, path modes, and the explicit libvirt socket contract. A real unit proof leaves a child
   alive after the main process exits and verifies that evidence waits for the exact cgroup to empty.
   Reconcile-once unit tests pin its reconciler identity/environment, retained invocation result,
   ordering after worker readiness, evidence-before-reset transition, inactive/no-job verification,
   and failure-to-report transition. Two successful runs observe distinct invocation identifiers;
   crash cases cover before receipt, after receipt, after reset, and before the reset flag persists.
5. A disposable-Postgres process test starts one and then several real workers through the lifecycle
   seam, observes distinct active incarnations and worker-role connections, terminates them, and
   observes exact evidence. A systemd-hosted live proof exercises the real units.
6. Script and control-protocol tests prove root/direct mode rejection, exact peer admission, schema
   and size bounds, fixed workspace/unit derivation, lifecycle-digest mismatch refusal,
   migration/bootstrap/role ordering, exact worker count, durable one-run serialization, idempotent
   same-run retry, mismatched-run refusal, restart refusal on unresolved evidence, and no shared DSN
   reaches a daemon. Process-boundary tests inspect environments and open configuration paths to
   prove only the preparation oneshot receives migration/application role credentials, the reporter
   reads seeds only for its bounded lifetime, the cleanup helper removes only fixed role files, and
   the long-lived witness retains only its witness DSN. Unit-control tests pin the literal helper,
   server, reconciler, and bounded worker allowlist and reject every other unit. Crash tests cover
   atomic run-bundle publication, every per-slot receipt update, each slot of partial multi-worker
   start and stop, startup-failure evidence, safe report completion before seed deletion, each helper
   restart and non-secret receipt, bundle removal after a positive receipt, the last bundle removal
   before run `complete`, each operation-phase write, final cleanup, and completed-record retirement.
   An Ansible-hosted proof invokes every allowed operation as `github-runner`, rejects another UID,
   and proves the account still has no general sudo or arbitrary systemd control. A serialized
   interleaving test persists `failed/pending`, crashes the report helper, submits caller `stop`, and
   proves no state downgrade, unit reset, bundle deletion, or role cleanup occurs before the report
   retry publishes its manifest. Ownership tests distinguish GitHub run attempts, reject delayed
   duplicate starts after completion, and exercise local `up`, `status`, `report`, and `down` in
   separate shells through one atomically persisted nonce. Teardown tests prove healthy stop stages
   its baseline before signaling, and stop timeout, database outage, and cleanup-helper failure keep
   a publishable report with exact property-only failure augmentation.
7. Workflow/reporter tests prove both live jobs invoke failure-only diagnostics and redact bare
   worker credentials and role passwords, including values crossing the output cutoff. They cover
   hostile journal formatting, multiple sources, per-source and total bounds, malformed seed
   withholding, no symlink following, rejection of arbitrary units, and a partial-start failure that
   still emits redacted daemon logs after the witness and report helper restart. The same proof runs
   for clean and abnormal worker exits after `running` and distinguishes them from caller-requested
   normal stop. A two-run proof logs unique bare secrets in run one, cleans it, and proves run two's
   cursor/invocation-filtered report and no-invocation property fallback cannot emit them. Workflow
   tests prove both jobs stage before stop and a final post-stop failure step renders bounded
   manifest files into the job log with every hostile content line prefixed. A clean test followed
   by stop timeout or cleanup-helper failure must publish diagnostics rather than merely leave files
   in a runner workspace.
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
