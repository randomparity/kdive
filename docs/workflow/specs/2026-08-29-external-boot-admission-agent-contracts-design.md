# External-boot admission and agent contracts design

## Scope

Issue #2117 implements ADR-0583's server-side admission and agent-contract slice. It applies one
System-locked matrix to Run creation and install/boot, System lifecycle and control, snapshot and
capture, and DebugSession attach/detach. It also exposes the contributor release, project-admin
conflict resolution, and platform-admin orphan-repair contracts with uniform `ToolResponse`
envelopes.

Job execution, reconciliation, provider mutation, and provider recovery mechanisms remain owned by
#2118 and the provider issues. This slice persists and enqueues the minimum versioned BOOT-job
request accepted by ADR-0584; it does not register an external recovery handler and does not enable
worker claim of authority-marked jobs. Python 3.14, x86_64, and ppc64le remain supported and no
dependency is added. No new migration and no new ADR number are assigned.

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

## Blocked precondition — the activation lifecycle has no Python implementation

The design below was reviewed adversarially on 2026-09-02 and three of its load-bearing premises were
refuted against the tree. They are recorded here because they decide what this issue can ship, and
they are not visible from the migrations alone.

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

**Open decision, owned by the operator.** Issue #2117's frozen completion criteria require MCP
wrappers carrying "absolute deadline/retry/recovery contracts". With no truthful transition to
anchor it, there is no absolute deadline to return. Either that criterion is narrowed — the three
contracts ship as admission-and-authorization surfaces returning a truthful
`configuration_error` with `reason=recovery_executor_unavailable`, exactly as this design already
accepted for `ops.resolve_recovery_orphan` — or the executable half moves to whichever issue owns the
activation lifecycle and the server-side authority boundary. This design does not choose; the rest of
the document describes the matrix, which is buildable either way.

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
discarded. The admission service therefore exposes `next_actions_for(state) -> list[str]` beside the
table, and each call site passes it as `failure_from_error(..., suggested_next_actions=...)`.

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

### Recovery requests — blocked, see *Blocked precondition* above

The rest of this section describes the state-mutating form of release and conflict resolution. It is
**not buildable on this branch** and is retained only so the operator's decision has something
concrete to accept or narrow. Three of its steps are refuted: the marker of step 9 cannot be
constructed, the authority it names cannot be allocated by the server, and the transition of step 8
has no exit. Its idempotency claim is separately wrong — step 8 mints a fresh `attempt_id` and
step 9 embeds it in the `dedup_key`, so the dedup never fires on a retry; the repository's actual
idempotency seam is `keyed_mutation` (`src/kdive/mcp/tools/lifecycle/support/_idempotency.py:70`),
which stores the envelope, as `install_run` and `power_system` already use it.

`services/external_boot/recovery_requests.py` owns release and conflict-resolution admission. Under
the System lock, release requires contributor on the owning Run, an `active` activation, no active
System job, and no attaching or live DebugSession for the System regardless of owning Run. Conflict
resolution requires project admin, the literal operation `restore-recorded-source`, and the exact
current conflict composite-state identity. Each operation reads database `server_time`, computes one
absolute UTC `recovery_readiness_deadline` from a configured timeout, persists the transition
request through `begin_recovery_attempt`, and enqueues one idempotent `JobKind.BOOT` job carrying the
`external_boot_authority_v1` marker in the same transaction. Ordinary retry returns that same job and
never extends the deadline; `queue.enqueue` is idempotent by `dedup_key` construction, so a replay
returns the pre-existing row unchanged.

### Truthful maturity — why the enqueued job is not a phantom feature

The durable half of each contract is real on this branch: the `active -> recovering` transition, the
recorded attempt, the absolute deadline, the allocated authority, and the queued marked job all
commit atomically. The executing half is not: migration 0122 excludes marked payloads from
`claim_worker_job`, so no worker claims the row until #2118 enables it. A tool that presented that
row as an in-flight recovery would be telling an agent something false.

This design therefore uses the repository's own disclosure mechanism (ADR-0175) rather than inventing
one. `runs.release_external_boot` and `systems.resolve_external_boot_conflict` register with
`meta={"maturity": "partial", "maturity_detail": {"reason": ...}}`, the reason naming that the
recovery executor is delivered separately.

**The metadata is not the disclosure an agent reads.** FastMCP serializes the wrapper docstring and
`Field` descriptions into the tool schema; `meta.maturity` drives `just docs` reference generation
and the `[partial: <reason>]` tag the lifecycle-prompt registrar renders on a journey step
(`src/kdive/mcp/prompts/registrar.py:240-250`), and neither reaches an agent that calls the tool
without going through a prompt journey. So the schema-visible half is the docstring, and it carries
the disclosure in prose; the metadata is the machine-readable record beside it. Claiming otherwise
was an error in the earlier draft of this section.

`ops.resolve_recovery_orphan` is a platform-admin repair admission contract. No durable quarantine
record or executable repair mechanism exists yet, so it validates authorization and the bounded
repair reference and returns a truthful `configuration_error` with `reason=repair_executor_unavailable`
and recovery guidance. It never reports success and registers `partial` for the same reason. It is
registered rather than withheld because ADR-0583 makes it the only authorized disposition of a
quarantined orphan, and an agent that cannot discover it has no way to learn that.

### Agent surface

MCP wrappers live with the existing runs and systems registrars; the repair tool is a new
`mcp/tools/ops/external_boot.py` plane registered beside the other `ops` registrars. Wrapper
docstrings and every `Field` description state RBAC, admissible state, idempotency, and the full time
contract required by `AGENTS.md`: unit (seconds), reference clock (database `server_time`), scope
(one recovery attempt), consequence of violation (`recovering -> recovery_failed` with evidence
retained), and recovery action (a literal tool name). Successful release and conflict responses are
job envelopes enriched with `activation_id`, `activation_state`, `server_time`, and the absolute
`recovery_readiness_deadline`. Failure envelopes use the stable taxonomy and literal tool names only.

## Failure contract

- Foreign or missing objects fail as `configuration_error` without disclosing membership.
- A matrix denial is non-retryable `conflict`; callers follow the returned action for the current
  activation state.
- Active jobs or sessions block release before any transition or enqueue and identify only bounded
  object ids already authorized to the caller.
- Replayed release or conflict requests return the same job, attempt, and deadline.
- A changed conflict identity leaves `recovery_conflict` untouched and returns `conflict`.
- Queue or transaction failure rolls back the transition and the deadline together.
- The orphan-repair tool never reports success while its separately owned executor is absent.

## Threat model

**Boundary inventory.** This change adds three MCP entry points (`runs.release_external_boot`,
`systems.resolve_external_boot_conflict`, `ops.resolve_recovery_orphan`) and one server→worker queue
boundary (the marked `JobKind.BOOT` payload). It widens no existing boundary: the guarded call sites
gain a check and a lock, never a new caller or a new parameter.

**Actor model.** The untrusted party is an authenticated project member reaching the MCP transport
with Run/System ids, an idempotency key, and a conflict observation digest. Provider-host
administrators and database administrators remain trusted exactly as ADR-0584 states. The worker is
trusted to hold its incarnation credential; the authority host is trusted to hold its mTLS identity.

**Control per boundary.**

- Each new tool: existing project-membership resolution plus `require_role` /
  `require_platform_role` — contributor for release, project `admin` for conflict resolution,
  `PLATFORM_ADMIN` for repair. A foreign or unknown identifier returns `configuration_error` with no
  membership disclosure, which is the existing convention at every lifecycle tool.
- Input bounding: bounded Pydantic fields reject malformed or oversized ids, idempotency keys, and
  digests before any database read. The conflict operation is a closed literal, not free text.
- Concurrency: the System advisory lock plus the partial unique index on uncleaned activations
  prevent two Runs from both passing admission. Exact digest compare-and-set prevents an
  administrator from approving a composite state that changed after observation.
- Queue boundary: the payload receives only immutable ids, the server-computed operation identity,
  the attempt id, and the deadline. No credential, provider definition, command, path, or secret
  crosses it. The authority allocator revalidates the locked job and actor attempt in the database
  before any mutation.
- Response leakage: denial data is limited to the activation id, activation state, and owning Run id
  — all already authorized to a caller who can read the System.

**Explicitly out of scope.** Provider execution, journal integrity, reconciliation, orphan deletion
or adoption, and live-provider behavior are owned elsewhere and are not represented as completed
behavior here. Authority-host deployment hardening is ADR-0584's and #2150's. Denial-of-service
through repeated admitted-then-denied calls is bounded by the existing per-tool authorization and
idempotency paths and is not separately addressed.

## Verification

Table tests cover every activation state against every operation, owning versus other Run, and
cleaned terminal states. PostgreSQL tests prove reverse admission is atomic and race another Run's
install against release so exactly one side proceeds. Focused MCP tests cover RBAC, redaction,
idempotent replay, unchanged deadlines, conflict compare-and-set, literal next actions, wrapper
schemas, the declared `partial` maturity and its reason, and the truthful unavailable repair
response. Existing control, snapshot, vmcore, Run, and DebugSession tests gain negative cases at the
shared service boundary. Generated tool reference output is regenerated with `just docs`.
`just lint`, `just type`, focused `just test-verbose` commands, and `just ci` are the required gates.
Verification for this issue runs on x86_64; the native ppc64le proof is deferred to a separate later
run on native POWER hardware.
