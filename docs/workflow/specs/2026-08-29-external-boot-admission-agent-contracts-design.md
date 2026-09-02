# External-boot admission and agent contracts design

## Scope

Issue #2117 implements ADR-0583's server-side admission and agent-contract slice. It applies one
System-locked matrix to Run creation and install/boot, System lifecycle and control, snapshot and
capture, and DebugSession attach/detach. It also exposes the contributor release, project-admin
conflict resolution, and platform-admin orphan-repair contracts with uniform `ToolResponse`
envelopes.

**Under the operator's recorded scope amendment (issue #2117, 2026-09-02) the three contracts ship
as admission-and-authorization surfaces.** Each resolves its object, enforces RBAC, and runs the
same admission matrix every other mutating call site runs, then returns a truthful
`configuration_error` carrying `reason=recovery_executor_unavailable`. No tool commits an activation
transition, allocates authority, or enqueues a job, because no tool on this branch can complete one.
#2118 owns the executor and owns flipping these three tools from `configuration_error` to live.

Job execution, reconciliation, provider mutation, and provider recovery mechanisms remain owned by
#2118 and the provider issues. Python 3.14, x86_64, and ppc64le remain supported and no dependency
is added. No new migration, no new ADR number, and no new configuration setting are assigned.

## Verified preconditions

The tree already carries every dependency this slice builds on. Each was read on the refreshed base
before this design was accepted:

- `ExternalBootAuthorityMarkerV1` (`src/kdive/jobs/models.py:188`) is the versioned ADR-0584 marker,
  persisted under the payload key `external_boot_authority_v1`. No enqueue site writes it yet.
- Migration `0122_external_boot_authority.sql` supplies `allocate_external_boot_authority`,
  `acknowledge_external_boot_authority`, and `commit_external_boot_authority_result`, and patches
  `claim_worker_job` / `count_claimable_worker_jobs` / `complete_worker_job` / `fail_worker_job` to
  exclude marked payloads. Its own comment states marked work is "installed but deliberately not
  enabled for claim or generic finalization".
- Migrations `0123` and `0125` supply the authority journal head CAS, its bounded inventory, and
  worker peer authentication. `src/kdive/providers/external_boot_authority/` supplies the authority
  protocol, journal, mTLS transport, `ExternalBootAuthorityService.readiness`, and the host process
  wired as `python -m kdive external-boot-authority-host`.
- `ExternalBootActivationRepository` (`src/kdive/db/external_boot_activations.py:65`) owns the
  activation row and holds `LockScope.SYSTEM` in every method.
- `kdive.jobs.worker` already finalizes marked jobs through `queue.complete_external_boot` /
  `queue.fail_external_boot`.

## Settled precondition — the activation lifecycle has no Python implementation

The design was reviewed adversarially on 2026-09-02 and three of its load-bearing premises were
refuted against the tree. The operator then amended this issue's completion criterion on the same
day. The refutations stand as recorded facts; what changed is what this issue builds on top of them.
They are kept here because they are the reason the three contracts are non-executing, and they are
not visible from the migrations alone.

**1. Nothing in `src/` creates or transitions an external-boot activation.** `rg -n
'external_boot_activations' src/ --type py` matches only the repository module itself;
`ExternalBootActivationRepository` has zero importers outside `tests/`. Every one of its transition
methods — `create`, `transition`, `begin_recovery_attempt`, `finish_recovery_attempt`,
`record_conflict`, `release_reservation`, `mark_cleanup_complete` — is called from no production
code path. The table, the migrations, and the repository are installed; the lifecycle that drives
them is not.

**2. The server cannot allocate external-boot mutation authority.**
`allocate_external_boot_authority` (`0122_external_boot_authority.sql:322`) opens with
`IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN RAISE ... ERRCODE = '42501'`, and
its `p_job_id` / `p_attempt` parameters bind an already-claimed job attempt. All three 0122 authority
functions are role-gated to `kdive_worker` or `kdive_provider_authority`. The MCP server runs as
`kdive_server`, so the authority allocation this slice would need is available to the worker and
unavailable here.

**3. The authority marker cannot be constructed from the activation row.**
`ExternalBootAuthorityMarkerV1` requires `authority_instance` and `provider_kind`
(`src/kdive/jobs/models.py:188`). Neither `ExternalBootActivation` nor `ExternalBootReservation`
(`src/kdive/domain/external_boot_activation.py:226,354`) carries either field. The earlier draft of
this spec asserted both could be read from the activation and its reservation; that is false.

**What follows from them.** `runs.release_external_boot` as originally designed commits
`active -> recovering` and nothing on this branch can move an activation out of `recovering`: no
worker claims an authority-marked job, no reconciler sweep exists, no caller of
`finish_recovery_attempt` exists, and the deadline that would fail the attempt forward is #2118's.
The System would then be denied Run creation, install, boot, power, snapshot, and reprovision by this
issue's own matrix, with only `systems.teardown` remaining. That is a one-way door reached on the
happy path by an authorized contributor following the documented contract — strictly worse than the
phantom feature the earlier draft set out to avoid, and `partial` maturity does not disclose it.

The admission matrix itself is unaffected: it is a guard, it denies nothing while no activation
exists, and it denies correctly the moment the lifecycle that creates activations lands.

**The operator's decision, recorded 2026-09-02.** The "absolute deadline/retry/recovery contracts"
criterion is narrowed. The three contracts ship as admission-and-authorization surfaces returning a
truthful `configuration_error` with `reason=recovery_executor_unavailable`, matching the treatment
this design already accepted for `ops.resolve_recovery_orphan`. Activation lifecycle execution and
reconciliation stay with #2118, which also owns flipping these three tools live. The hard rule the
amendment adds: **no tool may commit an activation transition it cannot complete**, so the
`active -> recovering` one-way door above is not reachable from anything this issue registers.

That decision removes each refuted premise by removing its dependent, rather than by working around
it. Nothing here constructs an `ExternalBootAuthorityMarkerV1`, so premise 3 has no subject; nothing
calls `allocate_external_boot_authority`, so premise 2 has no subject; nothing calls
`begin_recovery_attempt`, so premise 1 has no subject and the one-way door has no entrance. The
`external_boot_marker` keyword on `queue.enqueue`, the
`EXTERNAL_BOOT_RECOVERY_READINESS_TIMEOUT_SECONDS` setting, and the recovery-deadline computation
that the earlier draft introduced are all dropped with the transition they existed to serve: each
had exactly one caller, and that caller is gone.

## Architecture

### One matrix

`services/external_boot/admission.py` is the only operation matrix. Callers hold the existing
`LockScope.SYSTEM` transaction lock before querying the one activation that is not fully cleaned;
the repository supplies that lookup. The decision accepts unrestricted work when no activation is
present or a terminal activation is cleaned. `preparing`, `prepared`, `activating`, `recovering`,
`recovery_conflict`, `recovery_failed`, and uncleaned terminal states admit only activation
continuation, reconciliation, conflict resolution, and teardown. `active` additionally admits
owning-Run debug attach/detach, traffic capture, force crash, and vmcore capture; it rejects another
Run and generic lifecycle, power, snapshot, or install work. Allowed provider-mutating active
operations return the activation identity and authority binding for the later execution mechanism;
observation stays on the existing read-only seams.

A denial is `ErrorCategory.CONFLICT`, carries only the authorized activation id/state and owning Run
id, and suggests `runs.get`; `active` also suggests `runs.release_external_boot`, while
`recovery_conflict` and `recovery_failed` suggest `systems.teardown`.

Those actions ride the envelope's own `suggested_next_actions` field, never
`CategorizedError.details`. `ToolResponse.failure_from_error` passes `exc.details` through
`safe_error_details` (`src/kdive/serialization.py:96`), which reduces every key to a JSON scalar and
drops non-scalars apart from three reserved list keys — so a list placed in `details` is silently
discarded. The admission service therefore raises `ExternalBootDenied`, a `CategorizedError` subclass
carrying the actions as an attribute, and each call site passes them through as
`failure_from_error(..., suggested_next_actions=exc.next_actions)`. Binding the actions to the error
that computed them is what keeps a call site from reporting one state's actions for another's denial.

### Reverse admission

Every reverse-admission caller invokes this service inside its System lock immediately before
durable admission or enqueue. `create_run`, `teardown_system`, `_reprovision_locked`,
`snapshot_system`/`restore_system`/`delete_snapshot`, `insert_session_locked`, and `detach_locked`
already hold `LockScope.SYSTEM` and gain only the guard call.

`install_run`, `boot_run`, `power_system`, `force_crash_system`, `capture_traffic`,
`diagnostic_sysrq_system`, and `_fetch_vmcore` do not hold it today. Each extends its existing lock
block to acquire `LockScope.SYSTEM` on the bound System **first**, before any `INVESTIGATION` or
`RUN` lock, because `src/kdive/db/locks.py` fixes the total order
`PROJECT → RESOURCE → ALLOCATION → SYSTEM → RECOVERY_STORE → INVESTIGATION → RUN`. Acquiring in any
other order against a peer that already takes `ALLOCATION → SYSTEM` (`_create_locked`) or
`SYSTEM → INVESTIGATION` deadlocks. A Run with no bound System cannot carry an external activation,
so the guard and the added lock are both skipped there rather than acquiring a lock on nothing.

The call-site set is the complete set of registered mutating tools ADR-0583:345-353 reaches, and it
is enumerable rather than narrative: every member of `ExternalBootOperation` has at least one
enforcing call site and one negative test. `services/runs/bind.py:_bind_locked` is in it and already
holds `SYSTEM` (`bind.py:151-154`). Read-only observation tools stay on their existing seams, which
is what ADR-0583 admits in `active`.

These new `conn.transaction()` blocks open on a pooled non-autocommit connection that has already
issued a read, so each is a **savepoint** rather than a top-level transaction
(`src/kdive/db/locks.py:126-135`, and the precedent at
`src/kdive/mcp/tools/catalog/artifacts/uploads.py:763`). Atomicity of the guard and the enqueue holds;
the consequence is that the advisory lock is released at end-of-request rather than end-of-block.
That is accepted because each block spans no external I/O — provider-resolver and refusal checks stay
outside it — and each site carries a comment saying so, since the behavior is invisible at the call
site.

### The three contracts — admission and authorization, no transition

`services/external_boot/recovery_requests.py` owns all three. Each is the same four-stage pipeline,
and the stages run in this order because each later stage would otherwise leak what an earlier one is
there to withhold:

1. **Resolve.** A malformed, missing, or out-of-project object id, and for release an unbound Run, is
   `configuration_error` with no membership disclosure — the existing convention at every lifecycle
   tool.
2. **Authorize.** `require_role(ctx, project, Role.CONTRIBUTOR)` for release,
   `Role.ADMIN` for conflict resolution, `require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)`
   for orphan repair.
3. **Admit.** Under `advisory_xact_lock(conn, LockScope.SYSTEM, system_id)`, run the same
   `check_external_boot_admission` every other call site runs — `EXTERNAL_BOOT_RELEASE` (owning-Run
   scoped, `active` only) and `EXTERNAL_BOOT_RESOLVE_CONFLICT` (System scoped, `recovery_conflict`
   only). A denial is the matrix's own `CONFLICT` envelope, identical to the one a racing Run's
   install receives. Orphan repair validates its bounded `object_identities` and closed `disposition`
   literal instead, because ADR-0583 scopes it to quarantined objects rather than to an activation.
4. **Report unavailable.** Return `ToolResponse.failure(..., ErrorCategory.CONFIGURATION_ERROR)`
   with `data={"reason": "recovery_executor_unavailable"}` and the literal next actions. The read
   transaction ends without writing.

Stage 3's System lock is taken for a read-only check, which looks unnecessary and is not: it is what
makes the denial mean the same thing as every other call site's denial. Without it, this tool could
report `active` while a concurrent `preparing` commit is mid-flight, and an agent would act on a
state the matrix had already left.

The amendment's hard rule is enforced structurally rather than by review: `recovery_requests.py`
imports nothing that can transition an activation. `ExternalBootActivationRepository` reaches it only
through `check_external_boot_admission`, which calls exactly one read method. A test asserts the
module's import closure contains no name that writes.

Release additionally reports the two blocking conditions ADR-0583 names, so the answer an agent gets
is the answer it will get once the executor lands: `CONFLICT` with
`data={"reason": "system_job_active", ...}` while any job for the System is `queued` or `running`,
and `CONFLICT` with `data={"reason": "debug_session_active", ...}` while any DebugSession for the
System is attaching or live, regardless of owning Run. Reporting these now is what distinguishes an
admission surface from a stub: a caller learns its request is inadmissible for a reason that outlives
the missing executor.

There is no `idempotency_key` parameter on any of the three. An idempotency key exists to make a
replayed **commit** return the first commit's envelope; none of these tools commits, so the key would
have nothing to key. #2118 adds it with the transition it belongs to.

### Truthful maturity — what an agent is told

No part of any of the three contracts executes on this branch, so the disclosure is not a nuance
about a queued job; it is the whole contract. Each registers `meta={"maturity": "partial",
"maturity_detail": {"reason": "degraded_stub", ...}}` under ADR-0175, the `detail` naming that the
tool validates authorization and admissibility and then reports the executor absent, and the
`promotion` naming #2118.

**The metadata is not the disclosure an agent reads.** FastMCP serializes the wrapper docstring and
`Field` descriptions into the tool schema; `meta.maturity` drives `just docs` reference generation
and the `[partial: <reason>]` tag the lifecycle-prompt registrar renders on a journey step
(`src/kdive/mcp/prompts/registrar.py:240-250`), and neither reaches an agent that calls the tool
without going through a prompt journey. So the schema-visible half is the docstring, and it carries
the disclosure in prose; the metadata is the machine-readable record beside it. Claiming otherwise
was an error in an earlier draft of this section.

The three are registered rather than withheld because ADR-0583 makes them the only authorized
dispositions of an active activation, a recovery conflict, and a quarantined orphan. An agent that
cannot discover them has no way to learn what the matrix's denials are steering it toward — the
matrix's own `suggested_next_actions` name `runs.release_external_boot` and `systems.teardown`, and
a next action naming an unregistered tool is the phantom feature this design is avoiding.

### Agent surface

MCP wrappers live with the existing runs and systems registrars; the repair tool is a new
`mcp/tools/ops/external_boot.py` plane registered beside the other `ops` registrars. Wrapper
docstrings and every `Field` description state the required role, the admissible activation state,
and — first, before either — that the tool does not perform the operation it names today: it
validates and reports, and returns `configuration_error` with `reason=recovery_executor_unavailable`
on an otherwise-admissible request.

`AGENTS.md` requires any stated limit to carry all five of unit, reference clock, scope, consequence,
and recovery action. **These wrappers state no limit**, because the amended contract has none: no
deadline is computed, no attempt is recorded, and no retry budget is consumed. Naming a deadline that
nothing enforces is exactly the phantom the amendment removed. What each docstring carries in its
place is the recovery action for the state it cannot act on — `systems.teardown` for a System stuck
in `recovery_conflict` or `recovery_failed`, and `runs.get` to observe — plus the named issue that
promotes the tool. When #2118 lands the executor and the deadline it enforces, the five-part limit
contract lands with it.

Every response from all three is a failure envelope. There is no success path to shape, so there is
no job envelope, no `activation_id` enrichment, and no `server_time`.

## Failure contract

- Foreign, missing, or malformed objects fail as `configuration_error` without disclosing membership.
- An RBAC denial is the existing `require_role` / `require_platform_role` envelope, unchanged.
- A matrix denial is non-retryable `conflict`; callers follow the returned action for the current
  activation state. A release or conflict-resolution denial is byte-identical in shape to the denial
  a racing Run's install receives, because it is the same code path.
- Active jobs or sessions block release with their own `reason` and identify only bounded object ids
  already authorized to the caller.
- An admissible request returns `configuration_error` with `reason=recovery_executor_unavailable`.
  None of the three ever reports success, and none writes.
- No tool commits an activation transition. The `active -> recovering` door has no caller.

## Threat model

**Boundary inventory.** This change adds three MCP entry points (`runs.release_external_boot`,
`systems.resolve_external_boot_conflict`, `ops.resolve_recovery_orphan`). It adds **no** queue,
provider, or authority boundary: nothing enqueues, nothing allocates authority, and nothing reaches a
provider. It widens no existing boundary — the guarded call sites gain a check and a lock, never a
new caller or a new parameter. Every one of the three new entry points is strictly deny-or-report;
none has a state-changing outcome, which removes the class of vulnerability that a partially
executable recovery path would have carried.

**Actor model.** The untrusted party is an authenticated project member reaching the MCP transport
with Run/System ids, a conflict observation digest, and a bounded list of recovery-object
identities. Provider-host administrators and database administrators remain trusted exactly as
ADR-0584 states.

**Control per boundary.**

- Each new tool: existing project-membership resolution plus `require_role` /
  `require_platform_role` — contributor for release, project `admin` for conflict resolution,
  `PLATFORM_ADMIN` for repair. A foreign or unknown identifier returns `configuration_error` with no
  membership disclosure, which is the existing convention at every lifecycle tool. Authorization runs
  **before** the admission read, so an unauthorized caller learns nothing about whether the System
  carries an activation.
- Input bounding: bounded Pydantic fields reject malformed or oversized ids and digests before any
  database read. The conflict operation and the repair disposition are closed literals, not free
  text, and `object_identities` is length-bounded both per element and in list length.
- Concurrency: the System advisory lock plus the partial unique index on uncleaned activations
  prevent two Runs from both passing admission at a guarded call site.
- Response leakage: denial data is limited to the activation id, activation state, and owning Run id
  — all already authorized to a caller who can read the System. The
  `recovery_executor_unavailable` response carries no object state at all.
- Reverse-admission call sites: each gains a read under a lock it either already held or now acquires
  ahead of its existing locks in the documented total order. None gains a new caller, parameter, or
  privilege, and a guard that raises leaves the site's own transaction rolled back to where it was.

**Explicitly out of scope.** Provider execution, journal integrity, reconciliation, orphan deletion
or adoption, and live-provider behavior are owned elsewhere and are not represented as completed
behavior here. Authority-host deployment hardening is ADR-0584's and #2150's. Denial-of-service
through repeated denied calls is bounded by the existing per-tool authorization path; each of the
three new tools performs at most one indexed single-row read under a per-System lock and writes
nothing, so it is a weaker amplifier than the lifecycle tools already exposed beside it.

## Verification

Table tests cover every activation state against every operation, owning versus other Run, and
cleaned terminal states. PostgreSQL tests prove reverse admission is atomic and race another Run's
install against a committing activation so exactly one side proceeds. Focused MCP tests cover RBAC,
redaction, literal next actions, wrapper schemas, the declared `partial` maturity and its reason, and
the truthful `recovery_executor_unavailable` response from each of the three tools.

Two tests exist specifically to hold the amendment's hard rule:

- an import-closure assertion that `services/external_boot/recovery_requests.py` reaches no
  activation-writing name, so a later edit that adds a transition fails a gate rather than shipping;
- a PostgreSQL assertion that calling all three tools against a seeded `active` activation leaves the
  activation row byte-identical, so the "no transition it cannot complete" rule is proven on the
  database rather than argued from the source.

Existing control, snapshot, vmcore, Run, and DebugSession tests gain negative cases at the shared
service boundary. Generated tool reference output is regenerated with `just docs`. `just lint`,
`just type`, focused `just test-verbose` commands, and `just ci` are the required gates.
Verification for this issue runs on x86_64; the native ppc64le proof is deferred to a separate later
run on native POWER hardware.
