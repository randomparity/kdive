# #3229 — proposed debug symbol staging ownership

Status: D1–D4 implementation approved under WORK:SCOPE6227985604; live proof remains gated.
Authority: exact WORK:SCOPE comment 6227985604, token q3229-dcd8db32; operator-approved
campaign review-plan SHA256 5ce7042937eaa47a8e1cfb10ba07265e720dac9be3d037e655b927ac17b29b50.
Base: main 0b177a335435d404191ecab13b3374f6c01ca136. Branch: fix/debug-staging-lifetime-3229.
Design denominator: M = 250 changed lines, live assessment in WORK:SCOPE; full-spec / iterating.
D1–D4 were explicitly approved for design/review through proposal SHA256
7e4a8f84ab8c30eb77601a97f310d6be5e2236bfc3680662940da634b728563b.

## Problem and evidence

C1 (#3229 Expected): reclaim session vmlinux after its GDB exits, on explicit end and abandoned
session reap. C2: ordinary temp usage depends on current attachments rather than their history.
C3: module staging has the same lifetime or another explicitly bounded schedule.
C4 (necessary preservation): failed attach retains existing private-path and cleanup protection.
The issue's proposed last-session-of-Run hook is a hypothesis, not a required interface.

## Approved choices

D1 attachment-owned lazy module roots replace run cache; D2 owner-process sweep/shutdown;
D3 confirmed-exit deletion, retained retryable owner and honest failure; D4 orderly-owner boundary.
Operator approval binds exact proposal7e4a8f84 for design/review only. ADR-0799 records alternatives.
D4 expressly excludes automatic post-SIGKILL/crash recovery and unkillable/unresponsive resources;
no existing debug-orphan issue owner is asserted, and no historic root scan is authorized.

## Contracts and mechanics

- An attachment owns one private mkdtemp root. Retain the exact created path on the attachment;
  never derive deletion targets from run/session IDs, transcript parents, client strings or glob.
- Transfer ownership on successful attach; attach failure removes the root only after any spawned
  controller has exited. Preserve 0700 roots, exact artifact-version reads and tar filter=data.
- Modules live under that root. Replace ModuleDebuginfoResolverSeam's (run_id, module) arguments
  with (attachment, module), an internal signature, and migrate real and fake callers together.
  Resolve run_id and owned root from attachment. No run cache or TTL remains. Extract to a
  temporary child then publish modules only after success; remove partial child on failure.
  Production resolver refuses a missing owned root; tests provide roots through fixtures.
- Engine attach already constructs an attachment before issuing MI setup. Make partial attachment
  ownership recoverable on setup failure, using a private typed attach-failure carrier containing
  that attachment and original exception. stage_and_attach binds its root before rethrowing;
  runtime keeps it cleanup-pending if exit is unconfirmed. Confirmed exit permits root removal
  and original failure propagation. A cleanup-pending attachment cannot serve engine operations;
  a live-row retry first completes its cleanup, then may attempt a fresh attach. Never serialize
  controller/path objects into tool envelopes.
  A failure before controller creation owns only a root. Reclaim it immediately; if removal
  fails, carry that root-only pending ownership to runtime for retry without inventing a controller.
- Runtime reap keeps the registry entry until exit and root removal both finish; repeated exit and
  already-absent directory are idempotent. Directory deletion error retains cleanup-pending state
  and a safe diagnostic. Live-call cleanup retries use the same typed pending reason/actions.
  One failed session cannot stop unrelated sweep cleanup.
- PygdbmiController.exit currently terminate/wait/communicate has no timeout (installed 0.11.0.0
  source inspected). Exit: terminate, wait up to 2 monotonic seconds, kill if needed,
  wait up to 2 further seconds. Successful return means the process was waited/reaped; failure
  retains ownership and raises infrastructure_failure. Fake controllers follow the same port
  promise. Closing controller pipes must not add an unbounded communicate wait after exit.
- Per-session lock serializes attach, operation and cleanup. Retain one lock identity while any
  caller/waiter holds it; a guarded WeakValueDictionary avoids history retention and split locks.
  Canonical str(session.id)/str(uid) keys every lock, registry lookup and reap; accepted UUID
  spellings affect response echoes only, never ownership identity.
  Recheck durable live state after obtaining the lock before a new attach/engine op.
- Cancellation of asyncio.to_thread does not stop its thread. Keep the lock until the dispatched
  work finishes even if the request is cancelled; propagate cancellation afterwards. Track that
  task through graceful shutdown from before dispatch, including first attaches not yet registered.
  Stop new dispatches, drain that active set, then snapshot and reap owned roots. Cleanup never
  races a thread still reading symbols.
- Owner sweep starts in the existing FastMCP lifespan, never during app construction or import.
  Inject one DebugRuntimeResolver into registration and lifespan, rather than construct two.
  It snapshots registered IDs (including cleanup-pending) and does not instantiate new runtimes
  while sweeping. Shutdown stops new operations, drains dispatched work, then reaps owned state.
- Sweep interval: 30 monotonic seconds between completed passes per server process;
  DB read timeout 5 seconds per batch; at most 100 local IDs per batch. Under responsive DB,
  scheduler, filesystem and exited/inactive operation, a detached attachment is attempted on the
  next completed pass after the 30-second delay; this is a retry cadence, not a wall-clock
  completion SLA. Active operations defer cleanup until their existing bounded calls finish.
  DB failure preserves files/ownership, logs a sanitized reason, retries next pass. An operator
  fixes backing-service/host failures then retries debug.end_session. No invented hard expiry.
- Do not reap when detach is denied or returns a failure envelope: the current lifecycle code
  invokes reap after any envelope, so bind cleanup to successful detach or fresh detached state.
  Preserve detached idempotence, roles/projects, transport selection and external-boot admission.

## Success and verification

For local-libvirt and configured remote-libvirt attachments in the named orderly deployment:
S1 (C1,C4): successful attach keeps symbols readable; explicit end exits then removes its root;
failed resolve/setup removes partial files after exit, and repeated end is a no-op after success.
S2 (C1): DB-detached/missing abandoned attachment is attempted by owner sweep without another
client call; live entries remain untouched; shutdown runs the same cleanup path.
S3 (C2,C3): repeated sequential attach/module-load/end cycles leave no completed roots; two
sessions of one Run retain independently readable module trees until their own cleanup.
S4 (C1 necessary safety): exit/remove failure retains a retryable owner and exposes failure;
request cancellation, queued ops and detach races cannot delete files still used by GDB/thread.
S5 (preservation): wrapper schema describes incomplete cleanup and recovery; no auth/role,
artifact-version, archive filtering, redaction or transport-policy regression.
Tests: deterministic fake boundaries, real temporary filesystem; events/barriers rather than
sleep-based races. Red/green leak assertion must fail against current main before fix.
Focused provider tests plus pure runtime lifecycle tests cover S1,S3,S4. Focused service-backed
end_session/owner-sweep selection tests cover S2,S5 after implementation authorization.
Whole-tree lint/type and staged hooks precede future commits; managed pre-push just ci owns
one final ordinary full run if shipping is authorized. No full gate for this design-only phase.
The #2813 quota-limited live rerun needs separate target/resource/cleanup approval and exact
candidate deployment proof; it remains not run. Native x86_64 and ppc64le remain separate targets;
unit ownership logic is architecture-neutral, not native POWER qualification evidence.

## Failure model

1. Actors and deployments
   - Authenticated contributors using gdbstub tools in an orderly long-running MCP server;
     local-libvirt or configured remote-libvirt; separate reconciler marks durable detach.
2. Invariants and assets at stake
   - No deletion before controller/thread quiescence; private symbol artifacts and host temp
     availability; no successful cleanup claim when owned resources remain.
3. Accepted failure classes — explicit D4 operator approval
   - Abrupt owner death and unkillable child/filesystem: no automatic cleanup guarantee in this
     non-durable model; retain files instead of unsafe delete. No historical orphan cleanup.
   - DB outage: cleanup delayed until state can be established; interval is not a hard deadline.
4. Covered elsewhere
   - Interrupt-stop reply consumption: #3230; between-call execution semantics: #3244;
     standalone stub probe: #3245; POWER GDB: #2739/#2740; native POWER proof: #2818;
     live quota proof: separately approved #2813 run. Durable debug-staging orphan recovery
     has no verified existing issue owner; D4 explicitly accepts this boundary, not a silent deferral.

## Threat model

- Boundaries added/widened: recursive deletion of owner-created local staging root; internal
  module resolver receives attachment rather than only run ID; no new network trust boundary.
- Actors: authenticated tenant can request attach/module operations; local OS operator controls
  temp parent; tenant supplies neither cleanup path nor arbitrary deletion target.
- Controls: create private 0700 root, retain exact ownership path, keep tar data filter and exact
  artifact lookup; project/contributor gate before runtime lookup, fresh live check under lock;
  delete only owned root after process/thread exit; diagnostic envelope never emits private path.
- Out of scope: privileged local tampering, abrupt process death recovery and filesystem failure
  beyond safe retention; existing artifact authorization/redaction remains owned by its guards.

## Compatibility, rollout and rollback

No migration, dependency, toolchain-floor change or deployed-state cleanup command. Internal
signatures migrate in one PR. New root ownership applies to new attachments only; existing leaked
roots are not claimed. Roll back code by revert; operator may end sessions before restarting,
but this preparation authorizes no restart or live cleanup. No legacy branch is retained.
D1–D4 implementation follows the explicit operator decision recorded in WORK:SCOPE6227985604.
Required live proof remains separately gated; no deployment or historical root cleanup is authorized.

Decision record: [ADR-0799](../../adr/0799-debug-symbol-staging-ownership.md).
