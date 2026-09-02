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

The one deliberately missing piece is worker claim of a marked job. That is #2118's, and it governs
the maturity decision below.

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

### Reverse admission

Every reverse-admission caller invokes this service inside its System lock immediately before
durable admission or enqueue. `create_run`, `teardown_system`, `_reprovision_locked`,
`snapshot_system`/`restore_system`/`delete_snapshot`, `insert_session_locked`, and `detach_locked`
already hold `LockScope.SYSTEM` and gain only the guard call.

`install_run`, `boot_run`, `power_system`, `force_crash_system`, and `_fetch_vmcore` do not hold it
today. Each extends its existing lock block to acquire `LockScope.SYSTEM` on the bound System
**first**, before any `INVESTIGATION` or `RUN` lock, because `src/kdive/db/locks.py` fixes the total
order `PROJECT → RESOURCE → ALLOCATION → SYSTEM → RECOVERY_STORE → INVESTIGATION → RUN`. Acquiring
in any other order against a peer that already takes `ALLOCATION → SYSTEM` (`_create_locked`) or
`SYSTEM → INVESTIGATION` deadlocks. A Run with no bound System cannot carry an external activation,
so the guard and the added lock are both skipped there rather than acquiring a lock on nothing.

### Recovery requests

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
recovery executor is delivered separately; the wrapper docstrings state plainly that the request is
recorded and queued and that the job stays `queued` until that executor lands. The lifecycle-prompt
registrar already renders a `[partial: <reason>]` tag from exactly that metadata, so the disclosure
reaches an agent through the schema it actually reads.

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
