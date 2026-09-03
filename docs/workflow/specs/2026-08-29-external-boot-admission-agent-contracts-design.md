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

**1. Nothing in `src/` creates or transitions an external-boot activation.** On the base this
design was written against, `rg -n 'external_boot_activations' src/ --type py` matched only the
repository module itself and `ExternalBootActivationRepository` had no importer outside `tests/`.
This change adds two — `services/external_boot/admission.py` and
`mcp/tools/external_boot/recovery_requests.py` — and both reach only the one read method,
`get_restricting_for_system`. The conclusion is unchanged: every transition method — `create`,
`transition`, `begin_recovery_attempt`, `finish_recovery_attempt`, `record_conflict`,
`release_reservation`, `mark_cleanup_complete` — is still called from no production code path, and
`tests/services/external_boot/test_recovery_requests.py` gates that for the contracts module. The
table, the migrations, and the repository are installed; the lifecycle that drives them is not.

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
continuation, reconciliation, conflict resolution, teardown, and debug detach.
`active` additionally admits owning-Run debug attach, traffic capture, force crash, crash watch, and
vmcore capture; it rejects another Run and generic lifecycle, power, snapshot, or install work.
Observation stays on the existing read-only seams.

Detach is admitted in every restricting state, not only `active`, and for any Run — the one
operation the matrix admits with neither fence. It is the reversal of an attach the matrix itself
admitted. Denying it once the activation leaves `active` would leave the session row `live` and its
gdbstub/drgn transport open on the provider host, with no action the agent holding the session can
reach — and that stranded session then blocks the release, which refuses on `debug_session_active`.
Dropping the owning-Run fence closes the same hole across Runs: that refusal is System-wide by
design (`active_session_ids_for_system` joins on `runs.system_id`), so a session owned by another
Run of the System would otherwise block the release and be detachable by nobody. `DEBUG_ATTACH`
keeps both fences. Both departures from ADR-0583:348-351 are recorded in
`docs/debt/0006-external-boot-detach-departs-from-adr-0583.md`, owned by #2118.

The guard is a guard and returns nothing: `-> None` on admission, raising `ExternalBootDenied`
otherwise. An earlier draft returned an `ExternalBootBinding` carrying the activation identity and
authority binding "for the later execution mechanism", but that mechanism is #2118's and nothing on
this branch consumes one, so the dataclass was scaffolding for work this issue excludes. #2118
introduces it with its first consumer.

A denial is `ErrorCategory.CONFLICT`, carries only a fixed `reason` of `external_boot_restricted`
plus the authorized activation id/state and owning Run id, and suggests `runs.get`; `active` also
suggests `runs.release_external_boot`, while `recovery_conflict` and `recovery_failed` suggest
`systems.teardown`. The `reason` is what the three recovery contracts set on every refusal they
raise, so an agent branching on `data.reason` reads the matrix denial the same way.

Those actions ride the envelope's own `suggested_next_actions` field, never
`CategorizedError.details`. `ToolResponse.failure_from_error` passes `exc.details` through
`safe_error_details` (`src/kdive/serialization.py:96`), which reduces every key to a JSON scalar and
drops non-scalars apart from three reserved list keys — so a list placed in `details` is silently
discarded. The admission service therefore raises `ExternalBootDenied`, a `CategorizedError` subclass
carrying the actions as an attribute, and each call site renders them through the shared
`mcp/tools/_common.py:external_boot_denial(object_id, exc, ctx)`. Binding the actions to the error
that computed them is what keeps a call site from reporting one state's actions for another's denial.

The render is not a pass-through: it drops the actions the caller cannot invoke, through the same
`mcp/exposure.py:visible_next_actions` (ADR-0261) every other breadcrumb producer uses. Without it a
project contributor denied on a `recovery_conflict` System is steered at `systems.teardown`, which
the ADR-0148 exposure filter hides from it and `require_role` denies at call time. The filter needs
a project, and two render frames — `runs.create`'s and `runs.bind`'s MCP adapters — hold none, so
the guard takes a required `project` argument and stamps it on the denial alongside the actions.

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

The call-site set is the complete set of registered mutating tools ADR-0583:345-353 reaches.
**That completeness is proven by a gate, not asserted.** A scope audit on 2026-09-02 refuted the
earlier narrative claim by finding three registered System-mutating tools outside the set, and the
gate that was supposed to catch that — "every member of `ExternalBootOperation` has an enforcing
call site" — was structurally blind to it, because a missing member is missing from both sides of
that comparison.

The gate is therefore inverted: a test enumerates every registered tool whose annotations mark it
mutating, and asserts each is either in the guarded set or in an explicit exemption mapping that
carries a reason. Adding a mutating tool without deciding its admission then fails a gate instead of
shipping.

Three sites the audit surfaced:

- `control.watch_for_crash` (`control/registrar.py:325`, enqueuing `JobKind.WATCH_FOR_CRASH`) —
  guarded as `SYSTEM_WATCH_CRASH`, admitted in `active` and denied in every other restricted state.
  ADR-0583's `active` clause admits read-only System observation and a crash watch is a console
  observer; its restricted-state clause rejects every capture and control operation, which is what a
  queued watch job is while an activation is mid-flight.
- `systems.authorize_ssh_key` (`systems/ssh_access.py:103`, enqueuing `JobKind.AUTHORIZE_SSH_KEY`) —
  guarded as `SYSTEM_AUTHORIZE_SSH_KEY`, admitted in no restricted state. Its own docstring calls it
  mutating; it writes the guest's authorized keys, which is exactly the System mutation the matrix
  exists to stop.
- `runs.cancel` (`runs/cancel.py:60`) — guarded as `RUN_CANCEL`, admitted in no restricted state.
  Its docstring promises that cancel "frees the System for a new `runs.create`", and with an
  uncleaned activation present it cannot: the matrix denies `RUN_CREATE`, so the System is not freed
  and the caller is not told why. Denying the cancel with the matrix's own next actions is what makes
  that promise honest.

`systems.check_ssh_reachable` (`ssh_access.py:157`) is **not** guarded, and that is a decision rather
than an omission. It registers `read_only()`, requires only `VIEWER`, and enqueues a banner-only
liveness probe that mutates nothing. ADR-0583 admits read-only System observation in `active` and
does not list observation among the classes its restricted states reject. It stays on its existing
seam with every other observation tool, and the exemption mapping records that reason.

`services/runs/bind.py:_bind_locked` is in the guarded set and already holds `SYSTEM`
(`bind.py:149-155`).

These new `conn.transaction()` blocks open on a pooled non-autocommit connection that has already
issued a read, so each is a **savepoint** rather than a top-level transaction
(`src/kdive/db/locks.py:126-135`, and the precedent at
`src/kdive/mcp/tools/catalog/artifacts/uploads.py:763`). Atomicity of the guard and the enqueue
holds; the consequence is that the advisory lock is released when the pooled connection's
transaction ends — at end-of-request — rather than at end-of-block.

The scope audit proposed calling `require_top_level_transaction` at each new block so the condition
fails fast. **That remedy is rejected with evidence: it would raise at every one of these sites by
construction.** Each handler reads the System or Run on the pooled connection before reaching the
block, so the connection is already `INTRANS` and the checker's precondition — a transaction-free
connection — is unsatisfiable without restructuring five handlers onto fresh connections. Its
docstring says it exists for a mint that must be visible to another process before a multi-GiB
write, which is not this.

What the concern is right about is the exposure *after* the block, and that is bounded by
construction here rather than by argument: at every one of these sites the guarded
`keyed_mutation(...)` is the handler's terminal statement, so the window between releasing the
savepoint and ending the request contains no further work. Each site carries a comment recording
both facts — that the block is a savepoint, and that nothing follows it — because neither is visible
at the call site, and a later edit that appends work after the block would silently extend a
System-wide lock. The implementation plan makes verifying "the enqueue is terminal" a per-site step
rather than a blanket claim.

### The three contracts — admission and authorization, no transition

`mcp/tools/external_boot/recovery_requests.py` owns all three. Each is the same four-stage pipeline,
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

   **A System with no activation is a denial for these two operations, and the guard cannot express
   that.** `check_external_boot_admission` returns `None` for an unrestricted System — that is what
   every reverse-admission call site needs, since an absent activation must admit ordinary work — and
   its denial carries `{reason, activation_id, activation_state, owning_run_id}`, three of which do
   not exist when there is no row. So the guard returning `None` is not success here: `request_release` and
   `resolve_conflict` each convert it into their own `CONFLICT`, with
   `data={"reason": "no_active_activation"}` and `data={"reason": "no_recovery_conflict"}`
   respectively, and `suggested_next_actions=["runs.get"]`. Without this the two tools would report
   `recovery_executor_unavailable` for a System with nothing to release — telling an agent its
   request was admissible when the object it named does not exist.
4. **Report unavailable.** Return `ToolResponse.failure(..., ErrorCategory.CONFIGURATION_ERROR)`
   with `data={"reason": "recovery_executor_unavailable"}` and the literal next actions. The read
   transaction ends without writing.

Stage 3's System lock is taken for a read-only check, which looks unnecessary and is not: it is what
makes the denial mean the same thing as every other call site's denial. Without it, this tool could
report `active` while a concurrent `preparing` commit is mid-flight, and an agent would act on a
state the matrix had already left.

The amendment's hard rule is enforced structurally rather than by review: `recovery_requests.py`
imports nothing that can transition an activation. `ExternalBootActivationRepository` reaches it only
through `check_external_boot_admission`, which calls exactly one read method. One test asserts no
activation-writing name is reachable in the module; a second pins its first-party import set to a
reviewed allow-list, which is the closure property — a name scan alone cannot see a write routed
through a new first-party helper.

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

MCP wrappers live with the existing runs and systems registrars; the repair tool joins
`mcp/tools/ops/security/breakglass.py`, which already hosts the platform-admin destructive System
repair tools `ops.force_teardown` and `ops.force_release` and already has a `register(app, pool)`.
An earlier draft created a new `mcp/tools/ops/external_boot.py` plane and an
`assembly/tool_registration.py` entry for one tool; extending the existing entry point costs neither.
Wrapper
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
- Release and cancel interlock. With an uncleaned activation the matrix denies `RUN_CANCEL`
  System-wide, and `request_release` refuses on any queued or running job for the System — so the
  two obvious moves are both closed and the only path out is `jobs.cancel` (deliberately unguarded,
  because cancelling is de-escalation) and then the release. The `system_job_active` refusal names
  `jobs.cancel` for exactly that reason; `jobs.wait` alone is a dead end for a job that will never
  run.
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
- Input bounding: the bounds are declared on the Pydantic fields — `max_length` on
  `observed_identity`, and on `object_identities` both per element and in list length — so an
  oversized value is rejected at the transport, before any database read. The handler re-checks the
  same bounds, which keeps them enforced on the direct service path too. The conflict operation and
  the repair disposition are closed literals, not free text.
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
through repeated denied calls is bounded by the existing per-tool authorization path and writes
nothing. Two of the three perform at most one indexed single-row read under the per-System lock.
`request_release` also runs the blocking-job query, and that query is **not** fully bounded. It is
two separately-planned arms rather than one `OR`, because an `OR` across an indexed and an
unindexed expression is planned as neither: measured on 200k `jobs` rows with both indexes present
and nothing matching, the single-statement form walked `jobs_pkey` end to end (201041 buffers,
`Rows Removed by Filter: 200000`) and never touched the expression index. Split, the
`payload->>'system_id'` arm plans as `Index Scan using jobs_payload_system_id_idx` and reads 3
buffers. The `run_id` arm has no covering index — none exists for `payload->>'run_id'` — so it
scans every job, and its `LIMIT` can end the scan early only when rows match, which the ordinary
no-blocker case is not. That residual scan runs under the per-System advisory lock and is the
weakest claim in this section: it is a real amplifier, not bounded work, and it is deferred to
#2118 with the index that would close it in
`docs/debt/0008-external-boot-release-job-scan-under-the-system-lock.md`. Until the executor lands
the tool refuses before any of this can be reached in anger.

## Verification

Table tests cover every activation state against every operation, owning versus other Run, and
cleaned terminal states. PostgreSQL tests prove reverse admission is atomic and race another Run's
install against a committing activation so exactly one side proceeds. Focused MCP tests cover RBAC,
redaction, literal next actions, wrapper schemas, the declared `partial` maturity and its reason, and
the truthful `recovery_executor_unavailable` response from each of the three tools.

Two tests exist specifically to hold the amendment's hard rule:

- two static assertions over `mcp/tools/external_boot/recovery_requests.py`: that it reaches no
  activation-writing name, and that its first-party import set equals a reviewed allow-list of
  `module:name` pairs. The name scan alone is one level deep, so a write routed through a new
  first-party helper would pass it; pinning the import set is the closure property that fails such
  an edit at the gate rather than letting it ship;
- a PostgreSQL assertion that calling all three tools against a seeded `active` activation leaves the
  activation row byte-identical, so the "no transition it cannot complete" rule is proven on the
  database rather than argued from the source.

Existing control, snapshot, vmcore, Run, and DebugSession tests gain negative cases at the shared
service boundary. Generated tool reference output is regenerated with `just docs`. `just lint`,
`just type`, focused `just test-verbose` commands, and `just ci` are the required gates.
### Guard placement against idempotent replay

The guard must never convert an idempotent replay into a refusal. `keyed_mutation` short-circuits
to `do_work()` when `idempotency_key is None`, so on the unkeyed path — the default for every tool
below — there is no stored envelope and the tool's dedup key is the only replay there is. A guard
ahead of it tells an agent its work was denied while the job it is polling stays queued and runs.

This table is the complete guarded set, classified by what a repeat call actually returns. It was
built by reading every site after three review rounds found the same defect at six, then four, then
three more sites: enumeration by discovery kept sampling the class instead of closing it.

| tool | dedup key | unkeyed repeat returns | placement |
| --- | --- | --- | --- |
| `control.power`, `control.diagnostic_sysrq`, `control.capture_traffic` | `…:{key or uuid4()}` | fresh work | guard decides, correctly |
| `control.force_crash` | `{sys}:force_crash`, `NEVER` | prior job | replay probed first |
| `control.watch_for_crash` | `{sys}:watch_for_crash`, `TERMINAL_OR_CANCELED` | prior job | replay probed first |
| `systems.authorize_ssh_key` | `{sys}:authorize_ssh_key:{fp}`, `NEVER` | prior job | replay probed first; no `idempotency_key` parameter exists, so this is its only replay path |
| `systems.snapshot` | `{sys}:snapshot:{name}`, `TERMINAL_OR_CANCELED` | prior job | `_snapshot_replay` ahead of the guard |
| `systems.delete_snapshot` | `{sys}:delete_snapshot:{name}`, `TERMINAL_OR_CANCELED` | prior job | replay probed first |
| `vmcore.fetch` | `{run}:capture_vmcore:{method}`, `NEVER` | prior job, terminal included | replay probed first |
| `systems.reprovision` | `{sys}:reprovision:{digest}` | prior job via the `REPROVISIONING` branch | guard below that branch |
| `systems.teardown` | `{uid}:teardown`, `NEVER` | prior job | replay and the `TORN_DOWN` short-circuit both ahead of the guard |
| `systems.restore` | `{uid}:restore:{name}:{paused}`, `TERMINAL` | **nothing** — the first restore leaves the System `RESTORING`, which the `READY` precondition rejects before the guard | not in the class |
| `runs.install`, `runs.boot` | `{run}:{step}`, `NEVER` when the step's `run_steps` row exists else `TERMINAL` | prior job — including a `succeeded` one under `NEVER` and a `canceled` one under `TERMINAL` | `_settled_replay` gates the guard with the policy the enqueue will use; the live-poll `_in_flight_job` stays narrow because `runs.install` uses it to short-circuit re-staging |
| `runs.create`, `runs.bind`, `runs.cancel`, `debug.*`, the two recovery contracts | — | enqueue no job | nothing to preempt |

Replay-eligible states are a function of the site's `recycle` policy, not a hand-listed set:
`queue.enqueue` defaults to `NEVER`, under which *every* prior row is returned unchanged including
a terminal one. `dedup_replay` derives them from the same enum `enqueue` branches on, because a
hand-listed set is exactly what got `vmcore.fetch` wrong on the first attempt.

Each probe asks about the exact dedup key its own `enqueue` will use, and runs unconditionally.
Scoping it on whether an idempotency key was *supplied* is the wrong question: what matters is
whether the key *varies* with it. Where it does (`control.power`, `control.diagnostic_sysrq`,
`control.capture_traffic`) a fresh key mints a new key with no prior row, so the probe finds
nothing and the matrix decides, correctly. Where the key is fixed, a fresh key cannot mint fresh
work and denying it is the same divergence as denying an unkeyed repeat. An earlier revision
scoped on the supplied-key question and left the defect open on the keyed path at three sites.

`test_every_guarded_tool_is_classified_for_the_replay_gate` derives its coverage from
`GUARDED_TOOLS` rather than a hand-written list, so a newly guarded tool fails the gate until it is
classified — the same inverted-gate technique that proves the matrix is closed.
`test_a_keyed_mutation_admitted_before_the_activation_still_replays` likewise decides from an
unrestricted control arm whether a tool's key varies, rather than hand-listing the two groups.

What the gates do and do not cover, measured rather than asserted:

- Disabling every dedup probe turns exactly the six affected tools red.
- The differential test decides "did the repeat mint a job?" from the `jobs` table, not from
  envelope equality — `runs.boot` replays the same job while flipping `data.replayed`, which an
  envelope comparison classified as fresh work and skipped.
- It still reaches only a **queued** prior job. The settled and canceled arms, which are the ones
  the step sites got wrong, are covered separately by
  `test_a_settled_step_repeat_still_replays_under_an_activation`.
- `systems.restore` is skipped because its repeat is refused by its own precondition, not because
  it replays; that is a real exclusion, not a gap.
- `_NO_JOB_PATH` is falsified for the three entries this module can invoke. The other four —
  `debug.start_session`, `debug.end_session`, and the two recovery contracts — rest on their
  stated reason alone.

Verification for this issue runs on x86_64; the native ppc64le proof is deferred to a separate later
run on native POWER hardware.
