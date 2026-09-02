# External-boot admission and agent contracts implementation plan

## Goal and architecture

Implement issue #2117 through one System-locked admission matrix and three truthful agent contracts.
Release and conflict resolution persist an idempotent ADR-0584 BOOT-job request under the System
lock; #2118 later enables worker claim and executes it.

Architecture: a new `kdive.services.external_boot` package holds a pure decision table
(`admission.py`) and a transactional request writer (`recovery_requests.py`). Every mutating
lifecycle call site calls the decision table inside the `LockScope.SYSTEM` transaction lock it
already holds — or is extended to hold — immediately before its durable enqueue or transition. Three
FastMCP wrappers expose release, conflict resolution, and orphan repair, each declaring ADR-0175
maturity metadata that tells an agent exactly how much of the path is executable today.

Tech stack: Python 3.14, psycopg 3, PostgreSQL, FastMCP, Pydantic, pytest, uv, and just.

Expected implementation size: 2000–2800 changed lines (L) — derived from the file map and task list
below: four service/db modules, six guarded call sites, three MCP wrappers with registration, one
config setting, regenerated reference docs, and their matching test files.

## Global constraints

- Support x86_64 and ppc64le and add no dependency. Verification for this issue runs on x86_64; the
  native ppc64le proof is deferred to a separate later run on native POWER hardware.
- Follow accepted ADR-0583 (external boot), ADR-0584 (authority fencing), and ADR-0175 (tool
  maturity). No new ADR number and no new migration number are assigned; write neither.
- Do not implement job handlers, reconciliation, or provider mechanisms. Do not modify migration
  `0122_external_boot_authority.sql` or enable worker claim of marked jobs — that is #2118's.
- Never report orphan repair success while its executor is unavailable.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` runs whole-tree (src + tests) with strict
  defaults.
- Doc-style guard: use **Milestone**, never "Sprint"; avoid "critical", "robust", "comprehensive",
  "elegant" in code comments, docstrings, and docs.
- Every `@app.tool` wrapper docstring and every `Field(description=...)` is the agent-facing
  contract; the inner handler's docstring is invisible to the agent. For any limit, state all five:
  unit, reference clock, scope, consequence of violation, and recovery action.
- Never pass a PR or issue body as a shell string; use `--body-file`.
- Guardrails: `just lint`, `just type`, `just test-verbose <path>`, and `just ci`. Run them bare.
- Branch: `feat/external-boot-admission-agent-contracts-2117`; base: `main`.

## Lock-order constraint (binds Tasks 2 and 3)

`src/kdive/db/locks.py:36` fixes the total co-hold order
`PROJECT → RESOURCE → ALLOCATION → SYSTEM → RECOVERY_STORE → INVESTIGATION → RUN`. Any call site
this plan extends acquires `LockScope.SYSTEM` **before** its existing `INVESTIGATION` or `RUN` lock.
`services/runs/admission.py:_create_locked` already documents why `ALLOCATION` precedes `SYSTEM`;
do not reorder it.

## File map

Created:

- `src/kdive/services/external_boot/__init__.py`
- `src/kdive/services/external_boot/admission.py` — the closed decision table.
- `src/kdive/services/external_boot/recovery_requests.py` — release and conflict-resolution writers.
- `src/kdive/mcp/tools/ops/external_boot.py` — the `ops.resolve_recovery_orphan` plane.
- `tests/services/external_boot/__init__.py`
- `tests/services/external_boot/test_admission.py`
- `tests/services/external_boot/test_recovery_requests.py`
- `tests/services/external_boot/test_reverse_admission.py`
- `tests/mcp/lifecycle/test_external_boot_contracts.py`

Modified:

- `src/kdive/db/external_boot_activations.py` — add the restricting-activation lookup.
- `src/kdive/jobs/queue.py` — one optional `external_boot_marker` keyword on `enqueue`.
- `src/kdive/config/core_settings.py` — add the recovery-readiness timeout setting.
- `src/kdive/mcp/tools/lifecycle/runs/steps.py` — guard install and boot; extend their locks.
- `src/kdive/mcp/tools/lifecycle/control/registrar.py` — guard power and force-crash; add locks.
- `src/kdive/mcp/tools/lifecycle/vmcore/handlers.py` — guard vmcore capture; add a lock.
- `src/kdive/mcp/tools/lifecycle/systems/admin.py` — guard teardown and reprovision.
- `src/kdive/mcp/tools/lifecycle/systems/snapshot.py` — guard snapshot, restore, delete.
- `src/kdive/services/runs/admission.py` — guard Run creation.
- `src/kdive/services/debug/lifecycle.py` — guard attach and detach.
- `src/kdive/mcp/tools/lifecycle/runs/registrar.py` — `runs.release_external_boot` wrapper.
- `src/kdive/mcp/tools/lifecycle/systems/registrar.py` — `systems.resolve_external_boot_conflict`.
- `src/kdive/mcp/assembly/tool_registration.py` — register the new `ops` plane.
- `docs/guide/reference/` and `docs/guide/reference/config.md` — regenerated, not hand-edited.

---

## Task 1: The closed admission matrix

Files: create `src/kdive/services/external_boot/__init__.py` and
`src/kdive/services/external_boot/admission.py`; modify
`src/kdive/db/external_boot_activations.py`; create `tests/services/external_boot/__init__.py` and
`tests/services/external_boot/test_admission.py`.

Where this fits: every later task calls into this module. Nothing else decides admissibility.

### Interfaces

Consumed from the existing codebase (each confirmed present at the stated path):

- `kdive.domain.capacity.state.ExternalBootActivationState` (`state.py:181`), a `StrEnum` whose
  members are exactly `PREPARING`, `PREPARED`, `ACTIVATING`, `ACTIVE`, `RECOVERING`, `RECOVERED`,
  `RECOVERY_CONFLICT`, `RECOVERY_FAILED`, `ABANDONED` with values `"preparing"`, `"prepared"`,
  `"activating"`, `"active"`, `"recovering"`, `"recovered"`, `"recovery_conflict"`,
  `"recovery_failed"`, `"abandoned"`.
- `kdive.domain.external_boot_activation.ExternalBootActivation` — the activation row model,
  carrying at least `id: UUID`, `system_id: UUID`, `run_id: UUID`, `state:
  ExternalBootActivationState`, and `cleanup_complete: bool`.
- `kdive.domain.errors.CategorizedError(message: str, *, category: ErrorCategory, details: dict[str,
  object] | None = None, terminal: bool = False)` (`errors.py:146`).
- `kdive.domain.errors.ErrorCategory.CONFLICT` (`errors.py:40`).
- `kdive.db.external_boot_activations.ExternalBootActivationRepository` (`external_boot_activations.py:65`).

Provided to later tasks:

```python
class ExternalBootOperation(StrEnum):
    RUN_CREATE = "run_create"
    RUN_INSTALL = "run_install"
    RUN_BOOT = "run_boot"
    SYSTEM_REPROVISION = "system_reprovision"
    SYSTEM_POWER = "system_power"
    SYSTEM_SNAPSHOT = "system_snapshot"
    SYSTEM_TEARDOWN = "system_teardown"
    FORCE_CRASH = "force_crash"
    CAPTURE_VMCORE = "capture_vmcore"
    CAPTURE_TRAFFIC = "capture_traffic"
    DEBUG_ATTACH = "debug_attach"
    DEBUG_DETACH = "debug_detach"
    EXTERNAL_BOOT_RELEASE = "external_boot_release"
    EXTERNAL_BOOT_RESOLVE_CONFLICT = "external_boot_resolve_conflict"


@dataclass(frozen=True, slots=True)
class ExternalBootBinding:
    """The activation an admitted operation must fence against, or None when unrestricted."""

    activation_id: UUID
    run_id: UUID
    state: ExternalBootActivationState


async def check_external_boot_admission(
    conn: AsyncConnection,
    system_id: UUID,
    operation: ExternalBootOperation,
    *,
    run_id: UUID | None = None,
) -> ExternalBootBinding | None: ...
```

`check_external_boot_admission` returns `None` when no activation restricts the System, returns the
binding when the operation is admitted against a live activation, and raises
`CategorizedError(category=ErrorCategory.CONFLICT, terminal=True)` otherwise. Its `details` carry
exactly `{"activation_id": str, "activation_state": str, "owning_run_id": str,
"suggested_next_actions": [...]}`.

New repository method:

```python
async def get_restricting_for_system(
    self, conn: AsyncConnection, system_id: UUID
) -> ExternalBootActivation | None: ...
```

It returns the single activation for `system_id` that is not fully cleaned — every row whose state is
in `{preparing, prepared, activating, active, recovering, recovery_conflict, recovery_failed}`, plus
`recovered` and `abandoned` rows whose `cleanup_complete` is false — and `None` otherwise. The
partial unique index from migration 0121 guarantees at most one such row.

### The table

Admitted operations by state, and nothing else is admitted:

| state | admitted operations |
|---|---|
| `preparing`, `prepared`, `activating`, `recovering` | `SYSTEM_TEARDOWN` |
| `recovery_conflict` | `EXTERNAL_BOOT_RESOLVE_CONFLICT`, `SYSTEM_TEARDOWN` |
| `recovery_failed` | `SYSTEM_TEARDOWN` |
| `active` | `EXTERNAL_BOOT_RELEASE`, `SYSTEM_TEARDOWN`, `FORCE_CRASH`, `CAPTURE_VMCORE`, `CAPTURE_TRAFFIC`, `DEBUG_ATTACH`, `DEBUG_DETACH` |
| `recovered` / `abandoned` with `cleanup_complete=false` | `SYSTEM_TEARDOWN` |
| no row, or terminal with `cleanup_complete=true` | every operation |

`RUN_CREATE`, `RUN_INSTALL`, `RUN_BOOT`, `SYSTEM_POWER`, `SYSTEM_REPROVISION`, and `SYSTEM_SNAPSHOT`
appear in no row above, so a restricting activation denies them in every state including `active` —
which is ADR-0583's "rejects every install or restage, unrelated-Run operation, generic
power/control operation, snapshot".

A second frozenset, `_OWNING_RUN_SCOPED`, holds `EXTERNAL_BOOT_RELEASE`, `CAPTURE_VMCORE`,
`CAPTURE_TRAFFIC`, `DEBUG_ATTACH`, and `DEBUG_DETACH`. An operation in that set is admitted only when
the caller's `run_id` equals the activation's `run_id`; a different or absent `run_id` is a denial.
`SYSTEM_TEARDOWN`, `EXTERNAL_BOOT_RESOLVE_CONFLICT`, and `FORCE_CRASH` are not in it: the first two
are System-scoped by ADR-0583, and `control.force_crash` takes only a `system_id`, so the only System
it can target while an activation is `active` is that activation's own.

Suggested next actions on denial: `["runs.get"]` always, plus
`"runs.release_external_boot"` when the state is `active`, plus `"systems.teardown"` when the state
is `recovery_conflict` or `recovery_failed`.

### Steps

1. Create `tests/services/external_boot/__init__.py` (empty) and write
   `tests/services/external_boot/test_admission.py` as a pure table test over a fake repository:
   parametrize every `(state, cleanup_complete, operation, owning_run)` combination against the
   table above and assert admit-or-`CONFLICT`, plus the exact `details` keys and
   `suggested_next_actions` for three representative denials. Run
   `just test-verbose tests/services/external_boot/test_admission.py` and expect collection to fail
   on the missing `kdive.services.external_boot.admission` import.
2. Add `get_restricting_for_system` to `ExternalBootActivationRepository`, following the existing
   `get` method's SQL and row-mapping style in the same file.
3. Write `src/kdive/services/external_boot/__init__.py` exporting `ExternalBootOperation`,
   `ExternalBootBinding`, and `check_external_boot_admission`, and
   `src/kdive/services/external_boot/admission.py` implementing the table as a module-level
   `Mapping[ExternalBootActivationState, frozenset[ExternalBootOperation]]` plus the
   owning-Run-scoped subset as a second `frozenset[ExternalBootOperation]`. Re-run the command in
   step 1 and expect exit 0.
4. Add a PostgreSQL test to the same file, using the `migrated_url` fixture re-exported by
   `tests/services/conftest.py`, proving `get_restricting_for_system` sees an uncleaned `recovered`
   row and ignores a `recovered` row whose `cleanup_complete` is true. Run
   `just test-verbose tests/services/external_boot/test_admission.py` and expect exit 0.
5. Run `just lint` and `just type`; expect exit 0. Commit.

Acceptance: one closed table decides every operation; denial output uses `ErrorCategory.CONFLICT`
and carries only the activation id, activation state, owning Run id, and literal next actions.

---

## Task 2: Apply reverse admission at every call site

Files: modify `src/kdive/services/runs/admission.py`,
`src/kdive/mcp/tools/lifecycle/runs/steps.py`,
`src/kdive/mcp/tools/lifecycle/control/registrar.py`,
`src/kdive/mcp/tools/lifecycle/vmcore/handlers.py`,
`src/kdive/mcp/tools/lifecycle/systems/admin.py`,
`src/kdive/mcp/tools/lifecycle/systems/snapshot.py`,
`src/kdive/services/debug/lifecycle.py`; create
`tests/services/external_boot/test_reverse_admission.py`; extend
`tests/mcp/lifecycle/test_control_registrar.py`, `tests/mcp/lifecycle/test_systems_snapshot.py`,
`tests/mcp/lifecycle/test_vmcore_tools.py`, `tests/mcp/lifecycle/test_runs_tools.py`, and
`tests/services/debug/test_detach.py`.

Where this fits: Task 1 supplies the decision; this task is the only place it is enforced.

### Interfaces

Consumed from Task 1: `check_external_boot_admission(conn, system_id, operation, *, run_id=None)`,
`ExternalBootOperation`, and `ExternalBootBinding` exactly as defined above.

Consumed from the existing codebase:

- `kdive.db.locks.advisory_xact_lock(conn, scope, key)` (`locks.py:94`) and `LockScope`
  (`locks.py:36`).
- `kdive.mcp.responses.ToolResponse.failure_from_error(object_id, exc, *, category=None,
  suggested_next_actions=None, data=None)` (`responses.py:278`).

Provided to later tasks: nothing. This task adds no new public name.

### Call sites and what each changes

Sites that already hold `LockScope.SYSTEM` gain only a guard call inside the existing lock block:

| file:function | operation passed | run_id passed |
|---|---|---|
| `services/runs/admission.py:_create_locked` (lock at :322-324) | `RUN_CREATE` | `None` |
| `mcp/tools/lifecycle/systems/admin.py:_teardown_locked` (lock at :360) | `SYSTEM_TEARDOWN` | `None` |
| `mcp/tools/lifecycle/systems/admin.py:_reprovision_locked` (lock at :132) | `SYSTEM_REPROVISION` | `None` |
| `mcp/tools/lifecycle/systems/snapshot.py:snapshot_system` (lock at :187), `restore_system` (:261), `delete_snapshot` (:385) | `SYSTEM_SNAPSHOT` | `None` |
| `services/debug/lifecycle.py:insert_session_locked` (lock at :95) | `DEBUG_ATTACH` | `request.run.id` |
| `services/debug/lifecycle.py:detach_locked` (lock at :172) | `DEBUG_DETACH` | the session's Run id |

`_teardown_locked` calls the guard for symmetry and for its returned binding; `SYSTEM_TEARDOWN` is
admitted in every state, so the call can only ever return, never raise.

Sites that must acquire `LockScope.SYSTEM` first:

- `mcp/tools/lifecycle/runs/steps.py:install_run` — the existing block at :179-184 is
  `conn.transaction(), advisory_xact_lock(INVESTIGATION, run.investigation_id),
  advisory_xact_lock(RUN, run.id)`. Insert `advisory_xact_lock(conn, LockScope.SYSTEM,
  run.system_id)` as the first lock in that same `async with`, guarded by `run.system_id is not
  None`. Call the guard with `RUN_INSTALL` and `run_id=run.id` immediately after `locked_run` is
  re-read and before the `queue.get_by_dedup_key` replay check.
- `mcp/tools/lifecycle/runs/steps.py:_enqueue_step` — the block at :437 is
  `conn.transaction(), advisory_xact_lock(RUN, run.id)`. Insert the `SYSTEM` lock first on the same
  condition, then call the guard with `RUN_BOOT` and `run_id=run.id`.
- `mcp/tools/lifecycle/control/registrar.py:power_system` — wrap the state re-read, guard call, and
  `keyed_mutation(...)` in `conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, uid)`,
  calling the guard with `SYSTEM_POWER` and `run_id=None`.
- `mcp/tools/lifecycle/control/registrar.py:force_crash_system` — same wrapping, calling the guard
  with `FORCE_CRASH` and `run_id=None`. `FORCE_CRASH` is not owning-Run scoped, so it is admitted in
  `active` and denied in every other restricted state.
- `mcp/tools/lifecycle/control/registrar.py:capture_traffic` (the handler enqueuing
  `JobKind.CAPTURE_TRAFFIC` at :498) — wrap its `_enqueue`/`keyed_mutation` pair in
  `conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, run.system_id)` and call the guard
  with `CAPTURE_TRAFFIC` and `run_id=uid`.
- `mcp/tools/lifecycle/vmcore/handlers.py:_fetch_vmcore` — wrap the `_enqueue`/`keyed_mutation` pair
  in `conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, run.system_id)` and call the
  guard with `CAPTURE_VMCORE` and `run_id=uid` inside it.

Each MCP-layer site converts the raised `CategorizedError` with
`ToolResponse.failure_from_error(<object_id>, exc)`, passing the same `object_id` that site already
uses for its other failures (`run_id` for run and vmcore tools, `system_id` for control, snapshot,
and admin tools). `services/runs/admission.py:_create_locked` and `services/debug/lifecycle.py` let
the error propagate to their existing callers, which already convert `CategorizedError`.

### Steps

1. Write `tests/services/external_boot/test_reverse_admission.py`: a PostgreSQL test over
   `migrated_url` that seeds an `active` activation owned by Run A and asserts each of Run B's
   install, boot, power, force-crash, snapshot, vmcore, and attach paths returns
   `error_category == "conflict"`, while Run A's force-crash, vmcore, attach, and detach succeed.
   Add a barrier race in the same file: two concurrent connections, one committing
   `active -> recovering` and one admitting Run B's install, asserting exactly one proceeds. Run
   `just test-verbose tests/services/external_boot/test_reverse_admission.py` and expect the
   negative cases to fail because no guard exists yet.
2. Add the guard call to the six already-locked sites listed above. Re-run the command from step 1;
   expect the snapshot, teardown, attach, and detach cases to pass and the rest to still fail.
3. Add the `SYSTEM` lock and guard to `install_run`, `_enqueue_step`, `power_system`,
   `force_crash_system`, and `_fetch_vmcore`, keeping `SYSTEM` first in every `async with`. Re-run
   the command from step 1 and expect exit 0.
4. Extend `tests/mcp/lifecycle/test_control_registrar.py`,
   `tests/mcp/lifecycle/test_systems_snapshot.py`, `tests/mcp/lifecycle/test_vmcore_tools.py`,
   `tests/mcp/lifecycle/test_runs_tools.py`, and `tests/services/debug/test_detach.py` with one
   negative case each asserting the `conflict` envelope and its `suggested_next_actions`. Run
   `just test-verbose tests/mcp/lifecycle tests/services/debug` and expect exit 0.
5. Run `just lint`, `just type`, and `just test-changed`; expect exit 0. Commit.

Acceptance: no reverse operation crosses a newly committed restriction, exactly one side of the race
proceeds, and no call site acquires `SYSTEM` after `INVESTIGATION` or `RUN`.

---

## Task 3: Persist release and conflict-resolution requests

Files: create `src/kdive/services/external_boot/recovery_requests.py`; modify
`src/kdive/config/core_settings.py` and `src/kdive/jobs/queue.py`; create
`tests/services/external_boot/test_recovery_requests.py`; extend `tests/jobs/test_queue.py`;
regenerate `docs/guide/reference/config.md`.

Where this fits: Task 4's wrappers call exactly these two functions. Nothing else writes a recovery
request.

### Interfaces

Consumed from the existing codebase (each confirmed present at the stated path):

- `kdive.jobs.queue.enqueue(conn, kind, payload, authorizing, dedup_key, *, max_attempts=DEFAULT_MAX_ATTEMPTS, recycle=JobRecyclePolicy.NEVER) -> Job` (`queue.py:63`). With the default
  `recycle`, a repeated `dedup_key` returns the pre-existing `Job` unchanged.
- `kdive.jobs.payloads.RunPayload(run_id: str)` — the `JobKind.BOOT` payload model
  (`payloads.py`, mapped at `:396`).
- `kdive.jobs.models.ExternalBootAuthorityMarkerV1` (`models.py:188`) with fields `activation_id:
  UUID`, `run_id: UUID`, `system_id: UUID`, `plan_identity: str` (pattern
  `^sha256:[0-9a-f]{64}$`), `purpose: Literal["activate","recover","resolve-conflict","release","teardown"]`,
  `provider_kind: Literal["local-libvirt","remote-libvirt"]`, `authority_instance: str`,
  `operation: Literal["activate","recover","resolve-conflict","release","cleanup","teardown","deadline","recovery-attempt","fail"]`,
  `operation_identity: str`.
- `kdive.domain.operations.jobs.JobKind.BOOT` (`jobs.py:27`).
- `kdive.mcp.tools._common.job_authorizing(ctx, project)` and `job_envelope(job, object_key,
  object_id)` (`_common.py:305`).
- `kdive.db.external_boot_activations.ExternalBootActivationRepository.begin_recovery_attempt(conn, *, system_id, activation_id, operation_owner_id, authority_generation, expected_state, attempt_id, recovery_readiness_deadline, resolution_operation=None, resolution_identity=None, acknowledged_composite_state=None) -> CasResult` (`external_boot_activations.py:595`) and
  `CasStatus.APPLIED` / `SUPERSEDED` / `NOT_FOUND` (`:33`).
- `kdive.security.authz.rbac.require_role(ctx, project, role)` (`rbac.py:136`) and
  `Role.CONTRIBUTOR` / `Role.ADMIN` (`rbac.py:26`).
- `kdive.services.debug.sessions.active_session_ids_for_system(conn, system_id) -> list[str]`
  (`sessions.py`).
- `kdive.config.registry.Setting` (`registry.py:35`).

Provided to Task 4:

```python
async def request_release(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    run_id: str,
    idempotency_key: str | None = None,
) -> ToolResponse: ...


async def resolve_conflict(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    system_id: str,
    operation: str,
    observed_identity: str,
    idempotency_key: str | None = None,
) -> ToolResponse: ...
```

Both return a job envelope on success, enriched via the `data` mapping with `activation_id`,
`activation_state`, `server_time`, and `recovery_readiness_deadline` (all ISO-8601 UTC strings for
the two time fields). Both return `ToolResponse.failure_from_error(...)` on a `CategorizedError`.

New setting, appended to `src/kdive/config/core_settings.py` following the shape of
`LIVE_SCRIPT_MAX_TIMEOUT_SECONDS` (`core_settings.py:459`) and added to the same module-level
registry list that setting is in:

```python
EXTERNAL_BOOT_RECOVERY_READINESS_TIMEOUT_SECONDS = Setting(
    name="KDIVE_EXTERNAL_BOOT_RECOVERY_READINESS_TIMEOUT_SECONDS",
    parse=_int,
    default="1800",
    group="jobs",
    processes=_SERVER,
    help=(
        "Seconds allowed for one external-boot recovery attempt, measured from the database "
        "server_time at which the attempt is recorded. The server computes one absolute UTC "
        "recovery_readiness_deadline from this value and returns it on the release and "
        "conflict-resolution envelopes; worker and job retries reuse it and never extend it. "
        "An attempt that reaches the deadline without readiness moves the activation to "
        "recovery_failed with its evidence retained (ADR-0583)."
    ),
    suggest="an integer number of seconds, e.g. 1800",
)
```

### Behavior

`request_release`:

1. Resolve the Run; a missing Run, a Run outside `ctx.projects`, or an unbound Run is
   `configuration_error` with no membership disclosure.
2. `require_role(ctx, run.project, Role.CONTRIBUTOR)`.
3. Open one transaction holding `advisory_xact_lock(conn, LockScope.SYSTEM, run.system_id)`.
4. `check_external_boot_admission(conn, run.system_id, ExternalBootOperation.EXTERNAL_BOOT_RELEASE,
   run_id=run.id)`. A non-`active` activation or a non-owning Run raises `CONFLICT` from Task 1.
5. Refuse with `CONFLICT` and `data={"reason": "system_job_active", "job_ids": [...]}` while any
   `JobKind` job for the System is `queued` or `running`, regardless of owning Run.
6. Refuse with `CONFLICT` and `data={"reason": "debug_session_active", "session_ids": [...]}` when
   `active_session_ids_for_system` is non-empty.
7. Read `server_time` with `SELECT clock_timestamp()` on the same connection and compute
   `deadline = server_time + timedelta(seconds=<setting>)`.
8. Call `begin_recovery_attempt` with `expected_state=ACTIVE`, a fresh `attempt_id`,
   `resolution_operation=None`, and that deadline. `CasStatus.SUPERSEDED` or `NOT_FOUND` raises
   `CONFLICT`.
9. `queue.enqueue(conn, JobKind.BOOT, RunPayload(run_id=str(run.id)), job_authorizing(ctx,
   run.project), f"{activation.id}:external_boot:release:{attempt_id}",
   external_boot_marker=marker)`, where `marker` is an `ExternalBootAuthorityMarkerV1` built from
   the activation row with `purpose="release"`, `operation="release"`, and
   `operation_identity=f"release:{activation.id}:{attempt_id}"`.

   `RunPayload` inherits `_PayloadBase`'s `extra="forbid"` (`payloads.py:34`), so the marker cannot
   ride inside it. Add one optional keyword to `kdive.jobs.queue.enqueue`:

   ```python
   external_boot_marker: ExternalBootAuthorityMarkerV1 | None = None
   ```

   When it is not `None`, `enqueue` merges `{"external_boot_authority_v1":
   external_boot_marker.model_dump(mode="json")}` into the `payload_json` dict that
   `dump_payload` returned (`queue.py:88`) before the `Jsonb(payload_json)` insert. That is the one
   place the sibling key can be written without loosening a payload model, `queue.py` already
   imports from `kdive.jobs.models`, and the replay path is unaffected: `ON CONFLICT DO NOTHING`
   leaves an existing marked row exactly as it was.
10. Return `job_envelope(job, "run_id", run.id)` with the four enrichment fields merged into `data`.

The whole of steps 3–9 is one transaction: a queue or CAS failure rolls the transition and the
deadline back together. A replay with the same idempotency key returns the recorded envelope through
the existing `keyed_mutation` path, so the deadline is never recomputed.

`resolve_conflict` is the same shape with these differences: it resolves the System rather than the
Run, requires `Role.ADMIN`, requires `operation == "restore-recorded-source"` exactly (anything else
is `configuration_error` with `reason=unsupported_resolution_operation`), passes
`ExternalBootOperation.EXTERNAL_BOOT_RESOLVE_CONFLICT`, requires the activation state to be
`recovery_conflict`, passes `observed_identity` as `acknowledged_composite_state` to
`begin_recovery_attempt` with `expected_state=RECOVERY_CONFLICT`, and uses `purpose="resolve-conflict"`,
`operation="resolve-conflict"`. A `SUPERSEDED` CAS means the observed identity no longer matches;
raise `CONFLICT` with `data={"reason": "observed_identity_stale"}` and leave the row untouched.

The marker's `plan_identity`, `provider_kind`, and `authority_instance` come from the activation row
and its reservation; read them rather than constructing them.

### Steps

1. Write `tests/services/external_boot/test_recovery_requests.py` over `migrated_url`: RBAC denial
   for viewer and for contributor-on-conflict-resolution; unbound and foreign Run
   `configuration_error`; non-`active` state `conflict`; active-job and active-session refusals with
   their exact `reason`; a stale `observed_identity` leaving `recovery_conflict` unchanged; a replay
   returning the same `job_id`, `attempt`, and `recovery_readiness_deadline`; and a forced queue
   failure leaving the activation state unchanged. Run
   `just test-verbose tests/services/external_boot/test_recovery_requests.py` and expect collection
   to fail on the missing module.
2. Add a case to `tests/jobs/test_queue.py` asserting that `enqueue(..., external_boot_marker=m)`
   stores `payload["external_boot_authority_v1"]` equal to `m.model_dump(mode="json")` and that a
   replay of the same `dedup_key` leaves it unchanged, and that omitting the keyword stores no such
   key. Run `just test-verbose tests/jobs/test_queue.py` and expect the new case to fail.
3. Add the `external_boot_marker` keyword to `kdive.jobs.queue.enqueue`. Re-run the command from
   step 2 and expect exit 0.
4. Add the setting to `src/kdive/config/core_settings.py`. Run `just config-docs` to regenerate
   `docs/guide/reference/config.md`, then `just config-docs-check`, `just config-guard`, and
   `just env-docs-check`; expect exit 0 from all three.
5. Write `src/kdive/services/external_boot/recovery_requests.py`. Re-run the command from step 1 and
   expect exit 0.
6. Run `just lint`, `just type`; expect exit 0. Commit.

Acceptance: queued rows carry a valid `ExternalBootAuthorityMarkerV1` under
`external_boot_authority_v1`; no tool claims worker execution exists; a rolled-back transaction
leaves neither a job nor a transition.

---

## Task 4: Expose the three MCP contracts

Files: modify `src/kdive/mcp/tools/lifecycle/runs/registrar.py`,
`src/kdive/mcp/tools/lifecycle/systems/registrar.py`,
`src/kdive/mcp/assembly/tool_registration.py`; create
`src/kdive/mcp/tools/ops/external_boot.py`; create
`tests/mcp/lifecycle/test_external_boot_contracts.py`; regenerate `docs/guide/reference/`.

Where this fits: the last task. It exposes Task 3's services and nothing new of its own.

### Interfaces

Consumed from Task 3: `request_release` and `resolve_conflict` exactly as defined above.

Consumed from the existing codebase:

- `kdive.mcp.tools._docmeta.mutating() -> ToolAnnotations` (`_docmeta.py:66`) and
  `maturity_meta(maturity) -> dict[str, object]` (`_docmeta.py:24`).
- `kdive.security.authz.rbac.require_platform_role(ctx, role)` (`rbac.py:203`) and
  `PlatformRole.PLATFORM_ADMIN` (`rbac.py:45`).
- The generator's maturity contract (`scripts/generate/gen_tool_reference.py:212`): a `partial`
  tool's `meta` must carry `maturity_detail` with a `reason` drawn from exactly
  `{"provider_support", "live_dependency", "unproven_worker_path", "operator_gate",
  "degraded_stub"}`, a non-empty `detail`, and a non-empty `promotion`. A non-`partial` tool must
  carry no `maturity_detail`. The generator raises on violation, so `just docs-check` fails
  independently of the suite.

Provided to later work: three registered tool names — `runs.release_external_boot`,
`systems.resolve_external_boot_conflict`, `ops.resolve_recovery_orphan`.

### Registration metadata

`runs.release_external_boot` and `systems.resolve_external_boot_conflict`:

```python
meta=_docmeta.maturity_meta("partial")
| {
    "maturity_detail": {
        "reason": "unproven_worker_path",
        "detail": (
            "The release request, its recovery deadline, and its authority-marked recovery "
            "job commit atomically, but no worker claims an authority-marked job yet: "
            "migration 0122 excludes that payload from claim_worker_job. The job stays "
            "queued until the external-boot recovery executor lands."
        ),
        "promotion": "Promoted when the external-boot recovery job handler and worker claim path land (#2118).",
    }
}
```

`ops.resolve_recovery_orphan` uses the same shape with `"reason": "degraded_stub"`, a `detail`
naming that no durable quarantine record exists yet so the tool validates authorization and the
repair reference and then returns `configuration_error` with
`reason=repair_executor_unavailable`, and the same `promotion` sentence.

`ops.resolve_recovery_orphan` takes `annotations=_docmeta.destructive()` and is added to
`_docmeta.DESTRUCTIVE_TOOLS` (`_docmeta.py:37`), because its promoted behavior deletes or adopts
quarantined recovery objects.

### Wrapper contracts

Each wrapper docstring states, in prose an agent reads at call time: the required role, the
admissible activation state, that the call is idempotent under `idempotency_key`, and the complete
time contract — the deadline is an absolute UTC timestamp, `data.server_time` is the reference
clock, the scope is one recovery attempt, reaching the deadline without readiness moves the
activation to `recovery_failed` with evidence retained, and the recovery action is
`systems.teardown`. Each also states that the returned job stays `queued` until the recovery
executor lands, so an agent does not poll it as an in-flight operation.

`Field` descriptions, one line each, no newlines (the generator rejects a newline in a parameter
description):

- `runs.release_external_boot(run_id, idempotency_key=None)`.
- `systems.resolve_external_boot_conflict(system_id, operation, observed_identity,
  idempotency_key=None)` — `operation`'s description names the single accepted literal
  `restore-recorded-source`; `observed_identity`'s names that it must equal the composite state
  identity returned by the most recent `systems.get`, and that a stale value leaves the conflict
  untouched.
- `ops.resolve_recovery_orphan(system_id, object_identities, disposition,
  idempotency_key=None)` — `disposition` accepts `delete` or `adopt`; `object_identities` is a
  bounded list of quarantined recovery-object identities.

### Steps

1. Write `tests/mcp/lifecycle/test_external_boot_contracts.py`: build the app through the existing
   assembly helper the other `tests/mcp/lifecycle` files use, then assert each tool is registered;
   its `meta["maturity"] == "partial"`; its `maturity_detail` passes
   `scripts.generate.gen_tool_reference._maturity_detail`; every parameter has a newline-free
   description; the docstring contains the words `server_time`, `deadline`, and `recovery_failed`;
   the RBAC denial envelope for each role boundary; the replay envelope equality; and that
   `ops.resolve_recovery_orphan` returns `error_category == "configuration_error"` with
   `data["reason"] == "repair_executor_unavailable"`. Run
   `just test-verbose tests/mcp/lifecycle/test_external_boot_contracts.py` and expect failure
   because no tool is registered.
2. Add the two lifecycle wrappers to `runs/registrar.py` and `systems/registrar.py`, matching the
   surrounding registration style (`runs.boot` at `runs/registrar.py:625` is the closest analogue).
3. Create `src/kdive/mcp/tools/ops/external_boot.py` with a `register(app, pool)` function and add
   `_pool_only_plane_registrar(ops_external_boot.register)` to `build_plane_registrars` in
   `src/kdive/mcp/assembly/tool_registration.py`, beside the other `ops` registrars. Add
   `"ops.resolve_recovery_orphan"` to `_docmeta.DESTRUCTIVE_TOOLS`.
4. Re-run the command from step 1 and expect exit 0.
5. Run `just docs` to regenerate the tool reference, then `just docs-check` and
   `just doc-constants-check`; expect exit 0. If `doc-constants-check` reports a stale tool count,
   run its generator without `--check` and commit the regenerated file.
6. Run `just lint`, `just type`, and `just test-changed`; expect exit 0. Commit.

Acceptance: the three tools are registered; their FastMCP schemas carry the complete unit, clock,
scope, consequence, and recovery contract; every response is a valid `ToolResponse`; and the
generated reference matches a fresh generation.

---

## Task 5: Integrate and verify

1. Run `just lint`, `just type`, and `just test-changed`; expect exit 0 from each.
2. Run the adversarial branch review and, because this diff adds MCP entry points and touches
   authorization, the security pass. Fix defensible in-scope findings and record any deferral in
   this plan's *Deferrals* section below.
3. Simplify without changing behavior, then re-run the focused gates.
4. Run `just ci` bare; expect exit 0.
5. Open a PR closing #2117, publish `WORK:REVIEW`, and hand off the exact merge-ready SHA without
   merging.

Rollback is `git revert` of the branch. Queued external requests are durable: before reverting a
deployed build, drain or execute them through #2118's executor, because reverting removes the only
tools that can create them but not the rows already created.

## Deferrals

None recorded yet. Any `$trial-loop` deferral from the design or branch review is appended here with
its owning record path or tracker issue.
