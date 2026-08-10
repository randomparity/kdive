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
  `6d4d89ca-849a-4668-95cf-c0cc2c64e4c1`.
- Interaction: interactive.
- Outcome: restore the supported host-process live-stack topology so local workers start through
  an authority-backed incarnation lifecycle and both live-VM CI jobs reach their tests.
- Criteria: supervise every configured worker; deliver a unique registered 256-bit credential
  privately; use role-specific database authorities; record termination and remove the handoff;
  preserve ADR-0533 for one and several workers; gate omissions before merge; expose redacted daemon
  logs when either live job fails.
- Provenance: issue #1926's Problem, Expected, and Proposed approach; accepted ADR-0533 and its
  accepted amendments.
- Exclusions: optional incarnation credentials, a shared static credential, weakened artifact
  fences, unrelated provider behavior, and unsupported deployment topologies.
- Surface: host lifecycle supervision; live-stack launcher, bootstrap, and environment wiring;
  existing incarnation and runtime-role seams; focused tests; live workflow diagnostics; required
  ADR and operator documentation; direct dependencies of those files.
- Ambiguities: none.

## Approaches

### Selected: dedicated witness plus per-worker guardians

Run one local lifecycle-witness daemon and one worker-side guardian per host worker. A guardian
starts its exact child behind an unreadable pipe and gives the witness an exact pidfd through a
private control socket. The witness derives the immutable Linux identity, mints and registers the
credential, then returns it for one-shot delivery through the pipe. The witness independently
observes pidfd termination while the guardian reaps its child.

The witness has only lifecycle-witness database authority. The guardian and worker have only worker
database authority. This reuses the existing local identity, role, worker authentication, and
termination functions without giving one long-running process both sides of the fence. It adds no
database migration and no shared secret path.

### Rejected: per-worker credential files

Unique directories could avoid the current fixed-path collision, but credential bytes would persist
until cleanup and the worker would still need a separate registration barrier. A configurable path
also widens the path-validation surface without solving ordering by itself.

### Rejected: reuse the managed Compose worker

The Compose gate already implements the authority protocol, but its worker cannot reach the host
local-libvirt and native live-test environment. Moving those authorities into the container would be
a different deployment design and is outside this repair.

## Architecture

### Local lifecycle witness and guardian

Add `kdive.processes.lifecycle.local_worker_lifecycle`, following the narrow, injectable style of
the Compose lifecycle module. It exposes fixed `witness` and `guard` modes rather than a public
arbitrary-command option. Guard mode always executes `<current-python> -m kdive worker`.

The launcher starts one witness daemon with only
`KDIVE_LIFECYCLE_WITNESS_DATABASE_URL`. The daemon owns a `0700` runtime directory and a private
Unix control socket, preconfigures the bounded worker slots, and accepts each slot once per launch
generation. It receives no worker DSN and makes itself non-dumpable before opening the database
credential so a same-UID worker cannot inspect its memory or `/proc` secret surfaces. A worker can
still kill a same-UID witness; that closes guardian connections and fails workers closed.

For every configured worker the launcher starts one guardian with only
`KDIVE_WORKER_DATABASE_URL`, the slot, and the control-socket path. Root and non-root worker modes
remain supported. The guardian:

1. creates a close-on-exec pipe and forks one worker child blocked on its read descriptor;
2. sets the child's parent-death signal and verifies the parent remains the guardian;
3. opens a pidfd for the exact child and passes it to the witness with `SCM_RIGHTS`;
4. keeps the control connection open while it waits and reaps the exact child; and
5. reports exit status only after the pidfd has become terminal.

For an unused configured slot, the witness:

1. validates the bounded control message, peer identity, slot, and received pidfd;
2. derives `hostname:pid:boot-id:start-ticks` from the live process;
3. creates a random lowercase-hex 256-bit credential;
4. registers the identity, local authority binding, hash, and current fence protocol;
5. returns exactly the credential bytes to the guardian after registration commits;
6. watches the pidfd independently of the guardian; and
7. records `succeeded`, `failed`, or `killed` only after pidfd termination.

The guardian writes the returned credential into the child pipe and closes its copy. Registration
failure closes the pipe without bytes and reaps the child. The witness accepts the guardian's exit
status only as diagnostic outcome input after independently observing death. Termination persistence
retries while Postgres is unavailable, leaving the witness visible to host lifecycle commands.

The guardian forwards SIGINT or SIGTERM to the exact child and continues reaping it. Loss of the
witness connection makes the guardian terminate and reap the child. Guardian death triggers the
child's parent-death signal; the surviving witness observes its pidfd. A witness crash can lose the
only authoritative handle and strand an incarnation if the worker also disappears before a witness
can reattach. The launcher reports that fail-closed state and refuses to overlap a replacement with
a surviving worker or guardian; it never converts process absence into evidence.

Supported providers do not detach artifact-consuming descendants. Protected install consumption
runs in a worker thread, and synchronous child commands are waited before their provider operation
returns. The exact worker process therefore remains the incarnation boundary defined by ADR-0533.

### Worker credential input

Add an optional worker-only setting naming an inherited credential descriptor. When present, the
worker reads at most the exact 64-character credential plus one overflow byte, requires lowercase
hex, closes the descriptor, and never falls back to the file on malformed or empty input. When
absent, Compose and Kubernetes keep reading the existing fixed private handoff file. One of these
authority transports remains mandatory.

The setting carries only a descriptor number, not credential material. It is documented in the
generated configuration reference and is set only by the local supervisor. The worker registers the
credential in its redaction registry before any database authentication attempt, as it does today.

### Host process and database-role wiring

`scripts/live-stack/env.sh` defines host-reachable defaults for the existing migration, server,
worker, reconciler, and lifecycle-witness login members. The existing shared development DSN remains
available to test and operator helpers that explicitly use it; no long-running process receives it.

Bring-up ordering becomes:

1. start the backend services and wait for Postgres;
2. apply migrations with the host migration-owner DSN;
3. run the existing idempotent local runtime-role bootstrap without starting the Compose app tier;
4. start server with only the server DSN and reconciler with only the reconciler DSN;
5. start one witness with only its DSN and the configured worker-slot count;
6. start each guardian and worker with only the worker DSN; and
7. settle on server, reconciler, witness, guardians, and every real worker process before inventory
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

- Missing witness DSN: the witness fails before opening its control socket and no guardian starts.
- Missing worker DSN: the guardian fails before forking a child; host bring-up names its log.
- Registration conflict or database failure: no worker authentication race; the pipe stays empty
  and the child is reaped.
- Missing, empty, oversized, malformed, or closed descriptor: worker startup fails without falling
  back to another credential source.
- Credential bound to another identity or protocol: existing worker validation rejects it before
  constructing the job worker.
- Child exits after registration: the witness records exact termination before releasing the slot.
- Termination database outage: the witness retries, graceful restart/down fails while it remains,
  and the evidence row is not invented or discarded.
- Guardian crash: its exact child receives the parent-death signal; the witness observes the pidfd
  and persists terminal evidence.
- Witness crash: each connected guardian terminates and reaps its worker. If the authoritative
  pidfd is lost before evidence commits, the fence remains pinned and launcher diagnostics forbid
  treating absence as termination.
- Multi-worker health-port conflict: the existing exact worker-count check fails bring-up and names
  the affected log; every successfully started worker still has a distinct credential and identity.
- Runtime-role bootstrap failure: no host process starts; the operator fixes the bootstrap or role
  DSN and reruns `up.sh`.
- Failure-log directory absent or empty: the diagnostic step states that no logs were produced and
  does not replace the job's earlier failure.

## Threat model

### Actors and trust

Untrusted actors are a compromised worker child, malformed inherited environment or descriptor
input, another unprivileged local process, and stale or conflicting database incarnation state. The
local operator account, root when root-worker mode is selected, migration owner, and
lifecycle-witness database role are trusted within this single-host development and CI topology.
The witness and guardian are separate compromise domains even when the operator runs them under one
OS account. GitHub-hosted and self-hosted live jobs are trusted workflow actors on their existing
event boundaries.

### Added boundaries

- **Witness → guardian → worker credential.** The inputs are the registered slot, pidfd, inherited
  descriptor, and 64 credential bytes. A private bounded control protocol, one-use slot, child-only
  descriptor, exact length and hex checks, and no fallback control it. A failure reaps the worker
  and reaches the redacting collector.
- **Witness → witness database.** The inputs are the derived identity, binding, hash, and outcome.
  The witness-only DSN, existing bounded SQL functions, independent pidfd observation, and exact
  binding reuse control it. A failure leaves the witness failed or retrying and creates no false
  evidence.
- **Launcher → long-running process.** The inputs are role-specific DSNs and process settings.
  Removing unrelated role variables and fixing the process command prevent credential bytes from
  reaching shell interpolation. Startup logs name missing or invalid configuration.

### Widened boundaries

- **Local process identity → incarnation row.** The input is live `/proc` identity owned by the
  spawned child. The guardian's parent-child handle, transferred pidfd,
  hostname/PID/boot/start tuple, and registration before handoff control it. A failure reaps the
  child and retains the immutable conflict.
- **Live CI → daemon logs.** The input is application and supervisor output. Regular-file checks,
  a 256 KiB per-file tail, and URL and secret-value redaction control it. A failure emits labelled,
  bounded text in the failed job.

The design reuses PostgreSQL role grants, credential hashing, current protocol checks, immutable
incarnation registration, termination functions, process start-tick identity, and application
redaction. It does not add an independent secret store or a second worker authentication mechanism.
The witness environment contains no worker DSN; guardian and worker environments contain no witness
DSN. A process receiving both would violate ADR-0536 rather than merely misconfigure this topology.

### Explicitly out of scope

- A malicious host root or migration owner can read process memory, descriptors, or database state;
  this local topology treats them as deployment authorities.
- Witness loss followed by worker loss before reattachment, host power loss, or the explicit
  force-stop path can strand an active incarnation and artifact fence. They cannot mark it
  terminated or release it.
- The change does not make the local `WorkerDeathVerifier` a durable recovery authority; the
  witness records termination only while it owns the pidfd.
- Compose and Kubernetes credential transports and their lifecycle authorities do not change.
- Failure-log reporting does not promise arbitrary application output is safe; it applies the named
  redactions and bound, while application logging remains responsible for not emitting new secrets.

## Executable acceptance proofs

1. Unit tests redden when registration does not precede credential delivery, when the pipe is empty,
   oversized, malformed, or unavailable, and when child identity or termination binding differs.
2. Witness and guardian tests prove a unique credential and identity per child, pidfd-before-handoff
   ordering, exact outcome mapping, signal and parent-death handling, termination retry, mutually
   exclusive role DSNs, and cleanup on registration failure.
3. A disposable-Postgres process test starts a real `python -m kdive worker` through one guardian
   and witness, observes an active registered incarnation and a live worker-role connection, then
   stops it and observes exact terminal evidence.
4. The same process test starts two workers on distinct aux ports, observes two active identities
   and credentials, and proves both terminate independently.
5. Script tests prove migration-before-bootstrap-before-process ordering, exact role DSNs for every
   process, unrelated-role scrubbing, witness and guardian use for every configured worker, and
   graceful refusal while evidence persistence is outstanding.
6. Workflow and log-reporter tests prove both live jobs run the failure-only step and that bounded
   output redacts URL credentials and secret-named values without following symlinks.
7. Focused Python, shell, configuration-doc, and workflow checks pass, followed by `just ci` with no
   warnings.

## Rollback

The change adds no schema migration. Before reverting, stop host workers through the supervisor and
confirm their incarnation rows are terminal. Reverting the code and scripts then restores the old
host launcher, but that launcher remains incompatible with mandatory incarnation credentials; it is
usable only for diagnosis, not as a supported worker path. Compose and Kubernetes deployments are
unchanged.
