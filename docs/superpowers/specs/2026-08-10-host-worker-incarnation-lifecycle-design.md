# Host worker incarnation lifecycle design

Issue: [#1926](https://github.com/randomparity/kdive/issues/1926)
Decision: [ADR-0555](../../adr/0555-authority-supervises-host-worker-incarnations.md)
Existing boundary: [ADR-0533](../../adr/0533-role-separated-worker-fence-evidence.md)
Branch: `feat/host-worker-lifecycle-1926`
Base: `main`
Guardrails: focused pytest during TDD; `just ci` before implementation and review commits
ADR index coupling: not coupled; `docs/adr/` is the index.

## Frozen authority

- Scope identity: `https://github.com/randomparity/kdive/issues/1926` plus token
  `a8cb0290-3b7a-4ace-b5d1-ead255606b3e`.
- Interaction: interactive.
- Outcome: restore the supported non-root host-process live-stack topology so local workers start
  through an authority-backed incarnation lifecycle and both live-VM CI jobs reach their tests;
  reject root-worker launch before startup with the non-root session-mode remedy.
- Criteria: supervise every configured non-root worker; deliver a unique registered 256-bit
  credential privately; use role-specific database authorities; record termination and remove the
  handoff; preserve ADR-0533 for one and several workers; reject root-worker mode before process
  launch with an actionable remedy; gate omissions before merge; expose redacted daemon logs when
  either live job fails.
- Provenance: issue #1926; accepted ADR-0533 and ADR-0536; user decision on 2026-08-10 to adopt the
  recommended non-root/session-only rescope.
- Exclusions: root-worker mode as a supported lifecycle topology; a new privileged provider helper;
  optional or shared incarnation credentials; weakened artifact fences; unrelated provider
  behavior; unsupported deployment topologies.
- Surface: non-root host lifecycle supervision; live-stack launcher, root-mode gate, bootstrap, and
  environment wiring; existing incarnation and runtime-role seams; focused tests; live workflow
  diagnostics; required ADR and operator documentation; direct dependencies of those files.
- Ambiguities: none.

## Approaches

### Selected: dedicated witness plus per-worker guardians

Run one local lifecycle-witness daemon and one worker-side guardian per host worker. A guardian
starts its exact child and retains a pidfd without reaping it. The non-dumpable child connects
directly to the witness, which binds the peer PID to its own pidfd, derives the immutable Linux
identity, and mints and registers the credential before direct delivery. The witness independently
observes pidfd termination; the guardian reaps only after evidence commits.

The witness has only lifecycle-witness database authority. The worker has only worker database
authority and the credential bound to its exact process. The guardian retains neither credential
after spawning and never receives the incarnation credential. This reuses the existing local
identity, role, worker authentication, and termination functions without giving one long-running
process both sides of the fence. It adds no database migration and no shared secret path.

The topology is non-root only. Both live jobs already use `qemu:///session` and
`KDIVE_WORKER_AS_ROOT=0`; they will run the worker as a dedicated `kdive-worker` service account
rather than the CI/operator account. Root mode and a same-UID operator worker cannot isolate a
co-resident witness from a compromised worker.

### Rejected: per-worker credential files

Unique directories could avoid the current fixed-path collision, but credential bytes would persist
until cleanup and the worker would still need a separate registration barrier. A configurable path
also widens the path-validation surface without solving ordering by itself.

### Rejected: reuse the managed Compose worker

The Compose gate already implements the authority protocol, but its worker cannot reach the host
local-libvirt and native live-test environment. Moving those authorities into the container would be
a different deployment design and is outside this repair.

### Rejected: retain host-root worker mode

A host-root worker can inspect, signal, trace, or replace any co-resident lifecycle process.
Distinct database settings do not preserve ADR-0536 against that authority. Supporting root-only
provider operations would require a separately designed constrained privileged interface, which the
rescope explicitly excludes.

## Architecture

### Local lifecycle witness and guardian

Add `kdive.processes.lifecycle.local_worker_lifecycle`, following the narrow, injectable style of
the Compose lifecycle module. It exposes fixed `witness` and `guard` modes rather than a public
arbitrary-command option. Guard mode always executes `<current-python> -m kdive worker`.

The launcher rejects `KDIVE_WORKER_AS_ROOT=1` before migrations, role bootstrap, or application
process launch and names `KDIVE_WORKER_AS_ROOT=0` with `qemu:///session` as the supported remedy.

Host provisioning supplies:

- a no-login `kdive-worker` account with no sudo or Docker access and read-only code/configuration;
- a distinct no-login `kdive-lifecycle` account for witness authority;
- a root-created socket group shared only for connecting to the bounded protocol;
- an authority-private `0700` state directory owned by `kdive-lifecycle`; and
- a traversable parent containing one group-connectable socket that the worker cannot replace.

The launcher validates account identity, memberships, directory ownership and modes, checkout
non-writability, absence of worker sudo/Docker authority, and session-libvirt reachability before
opening any database credential. Hosted CI provisions this boundary in its job setup; the
self-hosted runner declares it in the owning Ansible role.

The root-owned short-lived launcher then starts one witness daemon as `kdive-lifecycle` with only
`KDIVE_LIFECYCLE_WITNESS_DATABASE_URL`. The daemon owns a `0700` runtime directory and a private
Unix control socket, preconfigures the bounded worker slots, and accepts each slot once per launch
generation. It receives no worker DSN and makes itself non-dumpable before opening the database
credential. The worker account cannot read, signal, trace, or replace that process or its authority
state. It can only connect to the group-owned socket.

For every configured worker the launcher creates a random one-use slot capability and starts one
root-owned guardian with the worker DSN, slot, capability, control-socket path, and a launcher-life
pipe. The guardian:

1. forks one worker child, drops it to `kdive-worker`, and supplies only the worker DSN, one-use
   slot, socket path, and its session-libvirt environment;
2. sets the child's parent-death signal and verifies the parent remains the guardian;
3. removes the DSN from its retained environment and state before the child handoff begins;
4. opens and retains a pidfd for the exact child;
5. authenticates the one-use slot capability, transfers the pidfd with `SCM_RIGHTS`, and waits for
   the witness to accept that expected identity;
6. waits for EOF on the launcher-life pipe, proving the multi-role launcher exited, before releasing
   the child;
7. waits with `waitid(..., WNOWAIT)` so a terminated child remains inspectable; and
8. reaps only after the witness acknowledges durable terminal evidence.

For an unused configured slot, the witness:

1. validates the guardian's bounded control message, one-use capability, unused slot, and pidfd;
2. derives the expected `hostname:pid:boot-id:start-ticks` from that live handle;
3. validates the worker hello and requires `SO_PEERCRED` plus the live start tuple to match the
   expected handle;
4. creates a random lowercase-hex 256-bit credential;
5. registers the identity, local authority binding, hash, and current fence protocol;
6. returns exactly the credential bytes directly to the same peer after registration commits;
7. watches the pidfd independently of the guardian; and
8. records `succeeded`, `failed`, or `killed` only after pidfd termination.

Before connecting, the worker makes itself non-dumpable. Registration or delivery failure closes
the connection without a usable credential, and the guardian reaps the failed child after the
witness confirms no active registration needs terminal evidence. The witness accepts the guardian's
exit status only as diagnostic outcome input after independently observing death. Termination
persistence retries while Postgres is unavailable, leaving the witness visible to host lifecycle
commands.

The worker retains the witness connection after credential delivery and exits when it closes. The
guardian forwards SIGINT or SIGTERM to the exact child but retains a terminated child with
`WNOWAIT`. Guardian death triggers the child's parent-death signal; the surviving witness observes
its pidfd.

The witness holds a Postgres advisory singleton lock while serving a launch generation. After a
witness crash, connection loss makes each worker exit while its guardian preserves the pidfd and
unreaped process. A guardian does not reconnect while its worker can execute. The launcher starts a
replacement witness, which must acquire the singleton lock before binding the socket. Each guardian
then transfers its retained pidfd and holder for terminal-only adoption. The witness loads the
registered authority binding, compares it with the still-inspectable process identity, observes
pidfd termination, and commits evidence before acknowledging reap. Duplicate, live, mismatched, and
already-reaped adoptions fail closed. Startup resolves every retained slot before opening a new
generation. Simultaneous force loss of the witness and guardian can strand a fence, as can host
power loss, but ordinary witness restart retains the runtime evidence ADR-0533 requires.

Supported providers do not detach artifact-consuming descendants. Protected install consumption
runs in a worker thread, and synchronous child commands are waited before their provider operation
returns. The exact worker process therefore remains the incarnation boundary defined by ADR-0533.

### Worker credential input

Add worker-only settings naming the local witness socket and bounded slot. When present, the worker
makes itself non-dumpable, connects to the socket, and sends one bounded hello. It accepts at most
the exact 64-character lowercase-hex credential plus one overflow byte and never falls back to the
file on a socket, peer, malformed, empty, or oversized failure. It retains the socket as a witness
liveness channel. When the settings are absent, Compose and Kubernetes keep reading the existing
fixed private handoff file. One authority transport remains mandatory.

The settings carry no credential material. They are documented in the generated configuration
reference and set only by the local guardian. The worker registers the credential in its redaction
registry before any database authentication attempt, as it does today.

### Host process and database-role wiring

`scripts/live-stack/env.sh` defines host-reachable defaults for the existing migration, server,
worker, reconciler, and lifecycle-witness login members. The existing shared development DSN remains
available to test and operator helpers that explicitly use it; no long-running process receives it.

Bring-up ordering becomes:

1. reject root-worker mode with the non-root session-mode remedy;
2. validate the dedicated worker/lifecycle account and filesystem boundary;
3. start the backend services and wait for Postgres;
4. apply migrations with the host migration-owner DSN;
5. run the existing idempotent local runtime-role bootstrap without starting the Compose app tier;
6. start server with only the server DSN and reconciler with only the reconciler DSN;
7. start one witness with only its DSN and the configured worker-slot count, and resolve every
   retained guardian adoption before opening new slots;
8. start each guardian and worker with only the worker DSN; and
9. wait for the root launcher to exit, release each blocked worker, then settle on server,
   reconciler, witness, guardians, and every real worker process before inventory
   reconciliation.

External role provisioning remains supported: `KDIVE_LOCAL_ROLE_BOOTSTRAP=0` skips the fixed local
login bootstrap and requires operator-provided role DSNs. Runtime launch scrubs unrelated role DSNs
even when the invoking shell exports them.

Host process discovery adds witness and guardian argv as separate sets. Graceful stop asks the
witness to stop each registered slot, waits for guardians to reap workers and for the witness to
persist evidence, and refuses restart or backend teardown while any lifecycle process remains. The
explicit force path can kill a stuck lifecycle process; that is reported as a fail-closed bypass and
does not create termination evidence.

### Failure diagnostics in live CI

Add one bounded log-reporting script used by both live jobs. On a failed job it enumerates regular
files under `.live-stack-logs`, emits a labelled tail of at most 256 KiB per file, strips URL
userinfo and secret-named key/value fields, and succeeds cleanly when no logs exist. It never
follows symlinks or prints files outside that directory.

Each live job adds a final `if: failure()` step invoking the script. This runs after failures in the
single bring-up/test block, so worker registration, authentication, database-role, and later test
errors retain their daemon exception without changing the original failed step's verdict.

## Failure contracts

- Root-worker mode selected: bring-up fails before any application process starts and names
  `KDIVE_WORKER_AS_ROOT=0` with `qemu:///session` as the supported configuration.
- Missing account, unsafe membership, writable code/configuration, unsafe runtime ownership/mode, or
  unreachable session libvirt: preflight fails before opening database credentials and names the
  provisioning repair.
- Missing witness DSN: the witness fails before opening its control socket and no guardian starts.
- Missing worker DSN: the guardian fails before forking a child; host bring-up names its log.
- Registration conflict or database failure: no worker authentication race; the connection closes
  without credential bytes and the child is retained or reaped according to registration state.
- Slot capability replay, wrong peer PID, or start-tick mismatch: witness refuses delivery, retains
  immutable conflict evidence, and guardian reaps only after the registration state is resolved.
- Missing socket, bad peer, used slot, empty, oversized, malformed, or closed response: worker
  startup fails without falling back to another credential source.
- Credential bound to another identity or protocol: existing worker validation rejects it before
  constructing the job worker.
- Child exits after registration: the witness records exact termination before releasing the slot.
- Termination database outage: the witness retries, graceful restart/down fails while it remains,
  and the evidence row is not invented or discarded.
- Guardian crash: its exact child receives the parent-death signal; the witness observes the pidfd
  and persists terminal evidence.
- Witness crash: connection loss stops each worker while its guardian retains the pidfd and
  unreaped process. The restarted witness adopts the exact identity and persists evidence before
  acknowledging reap or replacement.
- Multi-worker health-port conflict: the existing exact worker-count check fails bring-up and names
  the affected log; every successfully started worker still has a distinct credential and identity.
- Runtime-role bootstrap failure: no host process starts; the operator fixes the bootstrap or role
  DSN and reruns `up.sh`.
- Failure-log directory absent or empty: the diagnostic step states that no logs were produced and
  does not replace the job's earlier failure.

## Threat model

### Actors and trust

Untrusted actors are a compromised worker child, malformed inherited environment or socket input,
another unprivileged local process, and stale or conflicting database incarnation state. The
local operator account, migration owner, and lifecycle-witness database role are trusted within
this single-host development and CI topology. Root is a trusted deployment authority. The witness,
guardian, and worker run under distinct privilege boundaries: the worker service account cannot
inspect, signal, trace, or modify the root launcher/guardian, lifecycle witness, their executable or
configuration, or their private runtime state. GitHub-hosted and self-hosted workflow processes are
trusted provisioning actors but do not run worker application code under their own account.

### Added boundaries

- **Guardian → witness expected identity.** The inputs are the slot, one-use capability, and pidfd.
  A private bounded control protocol, capability consumption, `SCM_RIGHTS`, and live immutable
  identity checks bind one configured child before it can run. A mismatch fails without credential
  delivery.
- **Witness → worker credential.** The inputs are the pre-registered pidfd, peer identity, and 64
  credential bytes. A private bounded socket protocol, `SO_PEERCRED` equality with the expected PID,
  live start-tick equality, non-dumpable endpoints, exact length and hex checks, and no fallback
  control it. The guardian never handles credential bytes. A failure retains or reaps the worker
  according to registration state and reaches the redacting collector.
- **Witness → witness database.** The inputs are the derived identity, binding, hash, and outcome.
  The witness-only DSN, existing bounded SQL functions, independent pidfd observation, and exact
  binding reuse control it. A failure leaves the witness failed or retrying and creates no false
  evidence.
- **Launcher → long-running process.** The inputs are role-specific DSNs and process settings.
  Per-role child environments, removal of unrelated variables, fixed commands, a launcher-life
  pipe, and release only after launcher exit prevent one long-running process from retaining both
  authorities. Startup logs name missing or invalid configuration.
- **Host privilege → worker.** The inputs are executable code, configuration, runtime paths, session
  libvirt, and the worker child environment. A dedicated no-login account, no sudo/Docker access,
  read-only code/configuration, protected lifecycle accounts and paths, and preflighted ownership
  prevent worker compromise from acquiring deployment or witness authority.

### Widened boundaries

- **Local process identity → incarnation row.** The input is live `/proc` identity owned by the
  spawned child. The guardian's parent-child handle, capability-bound transferred pidfd, witness
  peer check, hostname/PID/boot/start tuple, registration before handoff, and unreaped restart
  adoption control it. A failure retains the child and immutable conflict until evidence is
  resolved.
- **Guardian → replacement witness.** The input is the unreaped child's pidfd and registered holder.
  A database advisory singleton lock, terminal-only adoption, database binding comparison, and
  evidence-before-reap acknowledgment prevent duplicate or false restart evidence.
- **Live CI → daemon logs.** The input is application and supervisor output. Regular-file checks,
  a 256 KiB per-file tail, and URL and secret-value redaction control it. A failure emits labelled,
  bounded text in the failed job.

The design reuses PostgreSQL role grants, credential hashing, current protocol checks, immutable
incarnation registration, termination functions, process start-tick identity, and application
redaction. It does not add an independent secret store or a second worker authentication mechanism.
The witness environment contains no worker DSN; guardian and worker environments contain no witness
DSN. The guardian has no incarnation credential, and removes the worker DSN from retained state
before handoff. The trusted short-lived launcher exits before worker code is released. A process
receiving both database authorities, a guardian receiving the credential, or a worker account able
to modify authority code/state would violate ADR-0536 rather than merely misconfigure this topology.

### Explicitly out of scope

- Root-worker mode is not a supported lifecycle topology. Root-only provider operations need a
  separately authorized constrained interface; this change fails the mode before process launch.
- A malicious host root or migration owner can read process memory, descriptors, or database state;
  this local topology treats them as deployment authorities.
- Simultaneous force loss of witness and guardian, host power loss, or the explicit force-stop path
  can strand an active incarnation and artifact fence. They cannot mark it terminated or release
  it. An ordinary witness restart is covered by unreaped-process adoption.
- The change does not make the local `WorkerDeathVerifier` a durable recovery authority; the
  witness records termination only while it owns the pidfd.
- Compose and Kubernetes credential transports and their lifecycle authorities do not change.
- Failure-log reporting does not promise arbitrary application output is safe; it applies the named
  redactions and bound, while application logging remains responsible for not emitting new secrets.

## Executable acceptance proofs

1. Unit tests redden when registration does not precede credential delivery, when the socket
   response is empty, oversized, malformed, or unavailable, and when capability, expected pidfd,
   peer identity, or termination binding differs.
2. Witness and guardian tests prove a unique credential and identity per child,
   peer-pidfd-before-handoff ordering, direct witness-to-worker delivery, exact outcome mapping,
   signal and parent-death handling, termination retry, mutually exclusive role DSNs, and cleanup
   on registration failure.
3. Account-boundary tests prove the worker identity cannot read or write witness secrets, code,
   configuration, or private state; cannot signal or trace the witness; and has no sudo or Docker
   authority. The launcher-life proof fails if worker code runs before the launcher exits.
4. A disposable-Postgres process test starts a real `python -m kdive worker` through one guardian
   and witness, observes an active registered incarnation and a live worker-role connection, then
   stops it and observes exact terminal evidence.
5. The same process test starts two workers on distinct aux ports, observes two active identities
   and credentials, proves both terminate independently, and restarts the witness while a guardian
   retains an unreaped child to prove adoption precedes reap and replacement.
6. Crash-transition tests stop the witness before registration, after registration, after delivery,
   and after child exit; each case either completes authenticated adoption and evidence or retains
   the child and refuses replacement.
7. Script tests prove root mode fails before application launch, then prove
   migration-before-bootstrap-before-process ordering, exact role DSNs for every process,
   unrelated-role scrubbing, witness and guardian use for every configured non-root worker, and
   graceful refusal while evidence persistence is outstanding.
8. Workflow and log-reporter tests prove both live jobs run the failure-only step and that bounded
   output redacts URL credentials and secret-named values without following symlinks.
9. Hosted workflow setup and the self-hosted Ansible role provision the same dedicated accounts,
   memberships, runtime paths, and session-libvirt contract, and their first real preflight passes.
10. Focused Python, shell, configuration-doc, and workflow checks pass, followed by `just ci` with
    no warnings.

## Rollback

The change adds no schema migration. Before reverting, stop host workers through the supervisor and
confirm their incarnation rows are terminal. Reverting the code and scripts then restores the old
host launcher, but that launcher remains incompatible with mandatory incarnation credentials; it is
usable only for diagnosis, not as a supported worker path. Compose and Kubernetes deployments are
unchanged.
