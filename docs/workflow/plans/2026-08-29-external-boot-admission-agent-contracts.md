# External-boot admission and agent contracts implementation plan

## Goal and architecture

Implement issue #2117 through one System-locked admission matrix and three truthful agent contracts.
Under the operator's recorded scope amendment (2026-09-02) the three contracts are
admission-and-authorization surfaces: they resolve, authorize, and admit, then return
`configuration_error` with `reason=recovery_executor_unavailable`. None commits an activation
transition, allocates authority, or enqueues a job. #2118 owns the executor and owns flipping them
live.

Architecture: a new `kdive.services.external_boot` package holds a pure decision table
(`admission.py`) and the three non-writing contract services (`recovery_requests.py`). Every mutating
lifecycle call site calls the decision table inside the `LockScope.SYSTEM` transaction lock it
already holds — or is extended to hold — immediately before its durable enqueue or transition. Three
FastMCP wrappers expose release, conflict resolution, and orphan repair, each declaring ADR-0175
`partial` maturity so an agent learns from the schema that the executor is absent.

Tech stack: Python 3.14, psycopg 3, PostgreSQL, FastMCP, Pydantic, pytest, uv, and just.

Expected implementation size: 1600–2200 changed lines (L) — derived from the file map and task list
below: two service modules, one repository read method, thirteen guarded call sites, three MCP
wrappers with registration, regenerated reference docs, and their matching test files. The amendment
removed the config setting, the `queue.enqueue` keyword, and the transactional request writer that
the pre-amendment estimate of 2000–2800 included.

## Global constraints

- Support x86_64 and ppc64le and add no dependency. Verification for this issue runs on x86_64; the
  native ppc64le proof is deferred to a separate later run on native POWER hardware.
- Follow accepted ADR-0583 (external boot), ADR-0584 (authority fencing), and ADR-0175 (tool
  maturity). No new ADR number and no new migration number are assigned; write neither.
- Do not implement job handlers, reconciliation, or provider mechanisms. Do not modify migration
  `0122_external_boot_authority.sql` or enable worker claim of marked jobs — that is #2118's.
- **No tool may commit an activation transition it cannot complete.** `recovery_requests.py` calls no
  activation-writing method, builds no `ExternalBootAuthorityMarkerV1`, and enqueues no job. Task 4
  step 6 holds this with a gate, not a convention.
- Never report release, conflict-resolution, or orphan-repair success while the executor is
  unavailable.
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

## Status: all tasks buildable under the recorded amendment

The 2026-09-02 adversarial review refuted three premises the pre-amendment Tasks 3 and 4 rested on.
The spec's *Settled precondition* section holds the evidence: nothing in `src/` creates or transitions
an external-boot activation, the server cannot allocate external-boot mutation authority (the 0122
functions are role-gated to `kdive_worker` and revoked from `kdive_server`), and the authority
marker's `authority_instance` and `provider_kind` are on neither the activation nor the reservation
row.

The operator then narrowed the criterion on the same day. Tasks 3 and 4 below are **rewritten** to
match: the three contracts validate and report, and the transition, the authority marker, the job
enqueue, the recovery deadline, and the configuration setting that served them are all removed. Each
refuted premise is answered by deleting its dependent rather than by working around it.

**Tasks 1 and 2 are unchanged** and were unaffected throughout. They are the matrix and its
enforcement — a guard that denies nothing while no activation exists and denies correctly once one
does.

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
- `tests/services/external_boot/conftest.py` — the shared activation-seeding factory.
- `src/kdive/services/external_boot/recovery_requests.py` — the three non-writing contract services.
- `src/kdive/mcp/tools/ops/external_boot.py` — the `ops.resolve_recovery_orphan` plane.
- `tests/services/external_boot/__init__.py`
- `tests/services/external_boot/test_admission.py`
- `tests/services/external_boot/test_recovery_requests.py`
- `tests/services/external_boot/test_reverse_admission.py`
- `tests/mcp/lifecycle/test_external_boot_contracts.py`

Modified:

- `src/kdive/db/external_boot_activations.py` — add the restricting-activation lookup.
- `src/kdive/mcp/tools/lifecycle/runs/steps.py` — guard install and boot; extend their locks.
- `src/kdive/mcp/tools/lifecycle/control/registrar.py` — guard power and force-crash; add locks.
- `src/kdive/mcp/tools/lifecycle/vmcore/handlers.py` — guard vmcore capture; add a lock.
- `src/kdive/mcp/tools/lifecycle/systems/admin.py` — guard teardown and reprovision.
- `src/kdive/mcp/tools/lifecycle/systems/snapshot.py` — guard snapshot, restore, delete.
- `src/kdive/services/runs/admission.py` — guard Run creation.
- `src/kdive/services/runs/bind.py` — guard Run bind.
- `src/kdive/services/debug/lifecycle.py` — guard attach and detach.
- `src/kdive/mcp/tools/lifecycle/runs/registrar.py` — `runs.release_external_boot` wrapper.
- `src/kdive/mcp/tools/lifecycle/systems/registrar.py` — `systems.resolve_external_boot_conflict`.
- `src/kdive/mcp/assembly/tool_registration.py` — register the new `ops` plane.
- `src/kdive/mcp/tools/_docmeta.py` — add `ops.resolve_recovery_orphan` to `DESTRUCTIVE_TOOLS`.
- `docs/guide/reference/` — regenerated, not hand-edited.

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
    RUN_BIND = "run_bind"
    RUN_INSTALL = "run_install"
    RUN_BOOT = "run_boot"
    SYSTEM_REPROVISION = "system_reprovision"
    SYSTEM_POWER = "system_power"
    SYSTEM_SNAPSHOT = "system_snapshot"
    SYSTEM_SYSRQ = "system_sysrq"
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
exactly the three scalar keys `{"activation_id": str, "activation_state": str, "owning_run_id": str}`
and nothing else.

The next actions are **not** in `details`: `ToolResponse.failure_from_error` runs `exc.details`
through `safe_error_details` (`src/kdive/serialization.py:96`), which reduces each value to a JSON
scalar and drops non-scalars apart from the reserved `errors` / `accepted_values` / `available` keys,
so a list there is silently discarded. They ride the raised error instead, on a `CategorizedError`
subclass this module owns:

```python
class ExternalBootDenied(CategorizedError):
    """A matrix denial, carrying the next actions `details` cannot carry."""

    def __init__(self, message: str, *, details: dict[str, object], next_actions: list[str]) -> None:
        super().__init__(message, category=ErrorCategory.CONFLICT, details=details, terminal=True)
        self.next_actions = next_actions
```

`next_actions` is `["runs.get"]`, plus `"runs.release_external_boot"` for `ACTIVE`, plus
`"systems.teardown"` for `RECOVERY_CONFLICT` and `RECOVERY_FAILED`. Every MCP call site passes it
through unchanged: `ToolResponse.failure_from_error(object_id, exc,
suggested_next_actions=exc.next_actions)`.

The actions travel on the error rather than through a `next_actions_for(state)` function every call
site would have to call with the right state, because the call sites do not have the state — they
have the exception. Binding the two together is what stops a site from reporting one state's actions
for another's denial.

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

`RUN_CREATE`, `RUN_BIND`, `RUN_INSTALL`, `RUN_BOOT`, `SYSTEM_POWER`, `SYSTEM_REPROVISION`,
`SYSTEM_SNAPSHOT`, and `SYSTEM_SYSRQ` appear in no row above, so a restricting activation denies them
in every state including `active` — which is ADR-0583's "rejects every install or restage,
unrelated-Run operation, generic power/control operation, snapshot".

A second frozenset, `_OWNING_RUN_SCOPED`, holds `EXTERNAL_BOOT_RELEASE`, `CAPTURE_VMCORE`,
`CAPTURE_TRAFFIC`, `DEBUG_ATTACH`, and `DEBUG_DETACH`. An operation in that set is admitted only when
the caller's `run_id` equals the activation's `run_id`; a different or absent `run_id` is a denial.
`SYSTEM_TEARDOWN` and `EXTERNAL_BOOT_RESOLVE_CONFLICT` are not in it: ADR-0583 scopes both to the
System.

**`FORCE_CRASH` is a stated residual, not an oversight.** ADR-0583:351 lists force-crash among the
`active`-admitted operations under an "owning-Run" modifier, but `control.force_crash` carries only a
`system_id` — there is no caller Run for the guard to compare, so passing one would mean inventing
it. This slice therefore admits `FORCE_CRASH` in `active` on the tool's own authorization —
project `ADMIN` plus the ADR-0130 destructive-op gate at `security/authz/gate.py` — and does not
enforce the owning-Run modifier. Enforcing it needs either a `run_id` parameter on
`control.force_crash` (a public contract change outside this issue's surface) or a rule tying
admission to a live DebugSession on the owning Run. Task 2's tests assert the admitted-in-`active`,
denied-in-every-other-restricted-state behavior that this slice does implement, and state the
unenforced modifier in a comment at the call site so it is not mistaken for coverage.

Suggested next actions on denial: `["runs.get"]` always, plus
`"runs.release_external_boot"` when the state is `active`, plus `"systems.teardown"` when the state
is `recovery_conflict` or `recovery_failed`.

### Steps

0. Create `tests/services/external_boot/__init__.py` (empty) and
   `tests/services/external_boot/conftest.py` holding one `seeded_activation` factory fixture that
   inserts an activation in a requested state — and, when asked, a `ready` reservation — satisfying
   every CHECK constraint in `0121_external_boot_activations.sql`. Tasks 1, 2, and 3 all consume it;
   without it their first steps have no way to reach a restricted state. Check
   `tests/db/external_boot_authority_support.py:191,232` first: the #2150 work already inserts
   activation rows there, so reuse or lift that helper rather than writing a second one. Run
   `just test-verbose tests/services/external_boot` and expect collection to succeed with no tests.
1. Write `tests/services/external_boot/test_admission.py` as a pure table test over a fake
   repository:
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
| `services/runs/admission.py:_create_locked` (lock at :320-325) | `RUN_CREATE` | `None` |
| `services/runs/bind.py:_bind_locked` (lock at :149-155) | `RUN_BIND` | the bound `run_id` param |
| `mcp/tools/lifecycle/systems/admin.py:_teardown_locked` (lock at :360) | `SYSTEM_TEARDOWN` | `None` |
| `mcp/tools/lifecycle/systems/admin.py:_reprovision_locked` (lock at :129-133) | `SYSTEM_REPROVISION` | `None` |
| `mcp/tools/lifecycle/systems/snapshot.py:snapshot_system` (lock at :184-188), `restore_system` (:258-262), `delete_snapshot` (:382-386) | `SYSTEM_SNAPSHOT` | `None` |
| `services/debug/lifecycle.py:insert_session_locked` (lock at :95) | `DEBUG_ATTACH` | `request.run.id` |
| `services/debug/lifecycle.py:detach_locked` (lock at :172) | `DEBUG_DETACH` | the session's Run id |

`_teardown_locked` calls the guard for symmetry and for its returned binding; `SYSTEM_TEARDOWN` is
admitted in every state, so the call can only ever return, never raise.

`detach_locked` is the one site that must widen a query to obtain its `run_id`: its `SELECT` at
:169-171 fetches `state, transport_handle, project` and no `run_id`, so `DEBUG_DETACH`'s owning-Run
comparison has nothing to compare. Add `run_id` to that select list; it is a column on
`debug_sessions` the row already has.

Sites that must acquire `LockScope.SYSTEM` first. The two `runs/steps.py` sites already hold locks
that come **after** `SYSTEM` in the total order, so the new lock goes at the head of the existing
`async with`. The four `control/registrar.py` sites and `_fetch_vmcore` hold **no** advisory lock
today — the survey confirmed `control/registrar.py` imports no `LockScope` at all — so each gains a
new `conn.transaction()` plus one `SYSTEM` lock around its guard and its existing
`keyed_mutation(...)` call:

- `mcp/tools/lifecycle/runs/steps.py:_restage_and_enqueue_install` (the locked body `install_run`
  delegates to via `keyed_mutation` at :115-122) — the existing block at :181-185 is
  `conn.transaction(), advisory_xact_lock(INVESTIGATION, run.investigation_id),
  advisory_xact_lock(RUN, run.id)`. Insert `advisory_xact_lock(conn, LockScope.SYSTEM,
  run.system_id)` as the first lock in that same `async with`. Call the guard with `RUN_INSTALL` and
  `run_id=run.id` immediately after `locked_run` is re-read at :186 and before the
  `queue.get_by_dedup_key` replay check.
- `mcp/tools/lifecycle/runs/steps.py:_enqueue_step` — the block at :443 is
  `conn.transaction(), advisory_xact_lock(RUN, run.id)`. Insert the `SYSTEM` lock first, then call
  the guard with `RUN_BOOT` and `run_id=run.id`.

Both are reached only after their caller has already returned `_not_bound(run_id)` for an unbound Run
(`install_run` at :107-108, `boot_run` at :299-300), so `run.system_id` is non-`None` at the lock and
no conditional acquisition is needed. Narrow it for the type checker with a local
`system_id = run.system_id` plus an `assert`-free early return rather than `AsyncExitStack`; the
conditional-lock machinery an earlier draft called for has no condition left to test.

- `mcp/tools/lifecycle/control/registrar.py:power_system` (:103-162) — wrap the guard call and the
  existing `keyed_mutation(...)` at :155-162 in `conn.transaction(), advisory_xact_lock(conn,
  LockScope.SYSTEM, uid)`, calling the guard with `SYSTEM_POWER` and `run_id=None`.
- `mcp/tools/lifecycle/control/registrar.py:force_crash_system` (:210-260) — same wrapping, calling
  the guard with `FORCE_CRASH` and `run_id=None`. `FORCE_CRASH` is not owning-Run scoped, so it is
  admitted in `active` and denied in every other restricted state.
- `mcp/tools/lifecycle/control/registrar.py:diagnostic_sysrq_system` (:263-322) — same wrapping,
  calling the guard with `SYSTEM_SYSRQ` and `run_id=None`. `SYSTEM_SYSRQ` is in no admitted row, so
  any restricting activation denies it.
- `mcp/tools/lifecycle/control/registrar.py:_capture_traffic` (:436-518, the handler behind
  `control.capture_traffic`) — wrap its guard and `keyed_mutation(...)` in `conn.transaction(),
  advisory_xact_lock(conn, LockScope.SYSTEM, system.id)`, using the `system` already fetched at :462
  after the unbound-Run check at :456-461, and call the guard with `CAPTURE_TRAFFIC` and
  `run_id=uid`.
- `mcp/tools/lifecycle/vmcore/handlers.py:_fetch_vmcore` (:195-294) — wrap its guard and the
  `keyed_mutation(...)` at :287-294 in `conn.transaction(), advisory_xact_lock(conn,
  LockScope.SYSTEM, system.id)`, using the `system` fetched at :229, and call the guard with
  `CAPTURE_VMCORE` and `run_id=uid`.

`_fetch_vmcore` is the MCP enqueue path, not the worker handler ADR-0562's `LockScope` note is
about. That note describes `capture_vmcore`'s **job handler** taking `RUN`, `RUN`, `SYSTEM`, `RUN` in
separate committed transactions; this site holds no other advisory lock, so the added `SYSTEM` lock
is held alone and creates no co-hold to order.

Where a site already opens `conn.transaction()` inside `keyed_mutation`, the new outer
`conn.transaction()` makes the inner one a **savepoint** (`src/kdive/db/locks.py:126-135`). The
guard and the enqueue stay atomic; the consequence is that the `SYSTEM` advisory lock now releases at
end-of-request rather than end-of-block. That is accepted because no site spans external I/O inside
the block — provider-resolver and refusal checks stay outside it — and each carries a comment saying
so, because the behavior is invisible at the call site.

Each MCP-layer site converts the raised `ExternalBootDenied` with
`ToolResponse.failure_from_error(<object_id>, exc,
suggested_next_actions=exc.next_actions)`, passing the same `object_id` that site already uses
for its other failures: `run_id` for the run and vmcore tools (`_fetch_vmcore` uses the raw
`run_id` string at :262, not `str(uid)`), and `system_id` for control (`diagnostic_sysrq_system`
uses the raw string at :292) and `str(system_id)` for admin (:174, :215). `snapshot.py` has no
`failure_from_error` call today and gains its first. `services/runs/admission.py:_create_locked`,
`services/runs/bind.py:_bind_locked`, and `services/debug/lifecycle.py` let the error propagate to
their existing MCP callers, which already convert `CategorizedError`.

### Steps

1. Write `tests/services/external_boot/test_reverse_admission.py` over `migrated_url`, using the
   `seeded_activation` factory Task 1 step 0 creates. Seed an `active` activation owned by Run A and
   assert each of Run B's create, bind, install, boot, power, sysrq, snapshot, reprovision,
   vmcore, traffic-capture, and attach paths returns `error_category == "conflict"` with the
   expected `suggested_next_actions`, while Run A's vmcore, traffic capture, attach, and detach
   succeed and Run A's install and boot are still denied (ADR-0583 rejects install and restage even
   for the owning Run). Assert `force_crash` is admitted in `active` and denied in
   `recovery_conflict`.

   Add a barrier race in the same file: two connections, the ordering forced by a
   `pg_advisory_xact_lock` on a test-only key rather than by timing — connection 1 takes the test
   key, commits `active -> recovering`, and releases; connection 2 blocks on it, then admits Run B's
   install. Assert exactly one side proceeds. Run
   `just test-verbose tests/services/external_boot/test_reverse_admission.py` and expect the
   negative cases to fail because no guard exists yet.
2. Add the guard call to the seven already-locked sites listed above. Re-run the command from step 1;
   expect the create, bind, snapshot, teardown, attach, and detach cases to pass and the rest to
   still fail.
3. Add the `SYSTEM` lock and guard to `_restage_and_enqueue_install`, `_enqueue_step`,
   `power_system`, `force_crash_system`, `capture_traffic`, `diagnostic_sysrq_system`, and
   `_fetch_vmcore`, keeping `SYSTEM` first in every `async with` and using `AsyncExitStack` where the
   lock is conditional. Re-run the command from step 1 and expect exit 0.
4. Extend `tests/mcp/lifecycle/test_control_registrar.py`,
   `tests/mcp/lifecycle/test_systems_snapshot.py`, `tests/mcp/lifecycle/test_vmcore_tools.py`,
   `tests/mcp/lifecycle/test_runs_tools.py`, and `tests/services/debug/test_detach.py` with one
   negative case each asserting the `conflict` envelope and its `suggested_next_actions`. Run
   `just test-verbose tests/mcp/lifecycle tests/services/debug` and expect exit 0.
5. Add a coverage assertion to `tests/services/external_boot/test_admission.py`: every member of
   `ExternalBootOperation` except `EXTERNAL_BOOT_RELEASE` and `EXTERNAL_BOOT_RESOLVE_CONFLICT`
   appears in the enforcing-call-site map the tests exercise. A site dropped in a later edit then
   fails a gate instead of shipping.
6. Run `just lint`, `just type`, and `just test-changed`; expect exit 0. Commit.

Acceptance: no reverse operation crosses a newly committed restriction, exactly one side of the race
proceeds, no call site acquires `SYSTEM` after `INVESTIGATION` or `RUN`, and every enforced operation
has a negative test.

---

## Task 3: The three admission-and-authorization services

Files: create `src/kdive/services/external_boot/recovery_requests.py`; modify
`src/kdive/services/debug/sessions.py` only if the System-scoped session lookup it already exposes
needs no change (verify first, do not add a second one); create
`tests/services/external_boot/test_recovery_requests.py`.

Where this fits: Task 4's wrappers call exactly these three functions and add nothing of their own.
Nothing else implements a contract.

**The amendment governs this task.** Each service resolves, authorizes, admits, and reports. None
writes. This task adds no configuration setting, no `queue.enqueue` keyword, no
`ExternalBootAuthorityMarkerV1`, no `begin_recovery_attempt` call, and no deadline — every one of
those existed in the pre-amendment draft solely to serve a transition that is now out of scope.

### Interfaces

Consumed from Task 1: `check_external_boot_admission`, `ExternalBootOperation`, and
`ExternalBootDenied` exactly as defined above.

Consumed from the existing codebase (each confirmed present at the stated path):

- `kdive.security.authz.rbac.require_role(ctx, project, role)` (`rbac.py:136`), `Role.CONTRIBUTOR` /
  `Role.ADMIN` (`rbac.py:26`), `require_platform_role(ctx, role)` (`rbac.py:203`), and
  `PlatformRole.PLATFORM_ADMIN` (`rbac.py:45`).
- `kdive.mcp.responses.ToolResponse.failure(object_id, category, *, detail, suggested_next_actions,
  data)` and `ToolResponse.failure_from_error(object_id, exc, *, category=None,
  suggested_next_actions=None, data=None)` (`responses.py:279`).
- `kdive.domain.errors.ErrorCategory.CONFLICT` / `CONFIGURATION_ERROR` (`errors.py:24,40`).
- `kdive.db.locks.advisory_xact_lock(conn, scope, key)` (`locks.py:94`) and `LockScope`
  (`locks.py:36`).
- `kdive.services.debug.sessions` — the System-scoped active-session lookup. **Read the module and
  use whatever it actually exports**; the pre-amendment draft cited
  `active_session_ids_for_system(conn, system_id) -> list[str]` without verifying it, and that
  citation is one of the four this design has already had to correct once. If no System-scoped
  lookup exists, write the query inline in `recovery_requests.py` rather than adding a second
  public name to `sessions.py`.

Provided to Task 4:

```python
async def request_release(
    pool: AsyncConnectionPool, ctx: RequestContext, *, run_id: str
) -> ToolResponse: ...


async def resolve_conflict(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    system_id: str,
    operation: str,
    observed_identity: str,
) -> ToolResponse: ...


async def resolve_recovery_orphan(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    system_id: str,
    object_identities: list[str],
    disposition: str,
) -> ToolResponse: ...
```

No `idempotency_key` parameter on any of the three: an idempotency key makes a replayed **commit**
return the first commit's envelope, and none of these commits. #2118 adds it with the transition it
belongs to.

### The shared unavailable response

One private helper builds the terminal response for all three, so the reason string and the
disclosure cannot drift apart:

```python
_UNAVAILABLE_REASON = "recovery_executor_unavailable"


def _executor_unavailable(object_id: str, tool: str) -> ToolResponse: ...
```

It returns `ToolResponse.failure(object_id, ErrorCategory.CONFIGURATION_ERROR, detail=<one
sentence naming the tool and that the external-boot recovery executor is not installed>,
suggested_next_actions=["systems.get"], data={"reason": _UNAVAILABLE_REASON})`.

The literal `recovery_executor_unavailable` is used by all three, including
`ops.resolve_recovery_orphan`. The pre-amendment draft gave the orphan tool its own
`repair_executor_unavailable`; the operator's amendment names one reason for all three, and one
reason is correct here because there is one missing thing — the external-boot recovery executor
#2118 owns.

### Behavior

`request_release`:

1. Parse `run_id`; a malformed id is `configuration_error`. Resolve the Run; a missing Run, a Run
   outside `ctx.projects`, or an unbound Run (`run.system_id is None`) is `configuration_error` with
   no membership disclosure.
2. `require_role(ctx, run.project, Role.CONTRIBUTOR)`.
3. Open one transaction holding `advisory_xact_lock(conn, LockScope.SYSTEM, run.system_id)`.
4. `check_external_boot_admission(conn, run.system_id, ExternalBootOperation.EXTERNAL_BOOT_RELEASE,
   run_id=run.id)`. A non-`active` activation, a non-owning Run, or no activation at all raises
   `ExternalBootDenied` from Task 1; convert it with `failure_from_error(run_id, exc,
   suggested_next_actions=exc.next_actions)`. Note that **no activation** is a denial here and an
   admission everywhere else: `EXTERNAL_BOOT_RELEASE` against a System with nothing to release is
   `conflict`, which Task 1's table already produces because the operation is owning-Run scoped and
   there is no owning Run to match.
5. Refuse with `CONFLICT` and `data={"reason": "system_job_active", "job_ids": [...]}` while any job
   for the System is `queued` or `running`, regardless of owning Run.
6. Refuse with `CONFLICT` and `data={"reason": "debug_session_active", "session_ids": [...]}` when a
   DebugSession for the System is attaching or live.
7. Return `_executor_unavailable(run_id, "runs.release_external_boot")`.

Steps 5 and 6 are kept from the pre-amendment draft deliberately. They are the two conditions
ADR-0583 names as blocking release, they are checkable today, and reporting them is what makes this
an admission surface rather than a stub: the caller gets the same refusal it will get once the
executor lands.

`resolve_conflict` is the same shape with these differences: it resolves the System rather than the
Run; requires `Role.ADMIN`; requires `operation == "restore-recorded-source"` exactly (anything else
is `configuration_error` with `data={"reason": "unsupported_resolution_operation"}`); passes
`ExternalBootOperation.EXTERNAL_BOOT_RESOLVE_CONFLICT`, which Task 1 admits only in
`recovery_conflict`; and has no job or session refusal, because a System in `recovery_conflict`
already fails the matrix for every operation that could start one. It does **not** compare
`observed_identity` against the stored composite state: that compare-and-set is one half of
`begin_recovery_attempt`, and calling it would commit the transition the amendment forbids.
`observed_identity` is accepted, bounded, and validated for shape only; the wrapper docstring says
exactly that, so an agent is not told a stale digest is being checked when it is not.

`resolve_recovery_orphan`: resolve the System; `require_platform_role(ctx,
PlatformRole.PLATFORM_ADMIN)`; validate `disposition in {"delete", "adopt"}` and that
`object_identities` is non-empty, within its length bound, and every element within its own length
bound; then return `_executor_unavailable(system_id, "ops.resolve_recovery_orphan")`. It runs no
admission check: ADR-0583 scopes it to quarantined recovery objects, which are not the activation the
matrix keys on, and no quarantine record exists on this branch to read.

### Steps

1. Read `src/kdive/services/debug/sessions.py` and record the exact System-scoped active-session
   symbol and signature it exports, or that it exports none. Do not cite an unread name.
2. Write `tests/services/external_boot/test_recovery_requests.py` over `migrated_url`, using the
   `seeded_activation` factory from Task 1 step 0: RBAC denial for viewer on release and for
   contributor on conflict resolution and on orphan repair; unbound, foreign, and malformed Run
   `configuration_error` with no membership disclosure; a non-`active` state and a non-owning Run
   both `conflict` with the expected `suggested_next_actions`; the active-job and active-session
   refusals with their exact `reason`; `unsupported_resolution_operation` for a non-literal
   operation; an out-of-bound `object_identities` rejection; and, for each of the three, an
   admissible call returning `error_category == "configuration_error"` with
   `data["reason"] == "recovery_executor_unavailable"`. Run
   `just test-verbose tests/services/external_boot/test_recovery_requests.py` and expect collection
   to fail on the missing module.
3. Add the amendment's hard-rule test to the same file: seed an `active` activation, read the whole
   `external_boot_activations` row, call all three services with admissible arguments, re-read the
   row, and assert it is unchanged field for field — including `state`, `current_attempt_id`, and
   `updated_at`. Assert `external_boot_recovery_attempts` and `jobs` gained no row for the System.
   This proves "no tool commits a transition it cannot complete" against the database rather than
   against the source.
4. Write `src/kdive/services/external_boot/recovery_requests.py`. Re-run the command from step 2 and
   expect exit 0.
5. Run `just lint` and `just type`; expect exit 0. Commit.

Acceptance: each service resolves, authorizes, and admits before reporting; every response is a
failure envelope; no activation row, recovery-attempt row, or job row is created or changed by any
of the three.

---

## Task 4: Expose the three MCP contracts

Files: modify `src/kdive/mcp/tools/lifecycle/runs/registrar.py`,
`src/kdive/mcp/tools/lifecycle/systems/registrar.py`,
`src/kdive/mcp/assembly/tool_registration.py`, `src/kdive/mcp/tools/_docmeta.py`; create
`src/kdive/mcp/tools/ops/external_boot.py`; create
`tests/mcp/lifecycle/test_external_boot_contracts.py`; regenerate `docs/guide/reference/`.

Where this fits: the last build task. It exposes Task 3's services and nothing new of its own.

### Interfaces

Consumed from Task 3: `request_release`, `resolve_conflict`, and `resolve_recovery_orphan` exactly as
defined above.

Consumed from the existing codebase:

- `kdive.mcp.tools._docmeta.mutating() -> ToolAnnotations` and `destructive() -> ToolAnnotations`
  (`_docmeta.py:66,62`), `maturity_meta(maturity) -> dict[str, object]` (`_docmeta.py:24`), and
  `DESTRUCTIVE_TOOLS` (`_docmeta.py:37`).
- The generator's maturity contract (`scripts/generate/gen_tool_reference.py:203-237`): a `partial`
  tool's `meta` must carry `maturity_detail` with a `reason` drawn from exactly
  `{"provider_support", "live_dependency", "unproven_worker_path", "operator_gate",
  "degraded_stub"}`, a non-empty `detail`, and a non-empty `promotion`. A non-`partial` tool must
  carry no `maturity_detail`. The generator raises on violation, so `just docs-check` fails
  independently of the suite.
- `_pool_only_plane_registrar` (`mcp/assembly/tool_registration.py:87-90`) and the `ops.*` plane list
  it feeds (`:285-311`).

Provided to later work: three registered tool names — `runs.release_external_boot`,
`systems.resolve_external_boot_conflict`, `ops.resolve_recovery_orphan`.

### Registration metadata

All three register `partial` with `reason: "degraded_stub"`. The pre-amendment draft gave release and
conflict resolution `unproven_worker_path`, which described a queued job no worker would claim; under
the amendment there is no job and no worker path, so `degraded_stub` is the accurate member of the
closed set:

```python
meta=_docmeta.maturity_meta("partial")
| {
    "maturity_detail": {
        "reason": "degraded_stub",
        "detail": (
            "Validates the caller's identity, role, and the System-wide external-boot "
            "admission matrix, then reports configuration_error with "
            "reason=recovery_executor_unavailable. No activation transition is committed "
            "and no recovery job is enqueued, because the external-boot recovery executor "
            "is not installed."
        ),
        "promotion": (
            "Promoted when the external-boot recovery job handler and worker claim path "
            "land (#2118)."
        ),
    }
}
```

`ops.resolve_recovery_orphan` uses the same shape with a `detail` naming that it validates the
platform role and the bounded repair reference before reporting the same reason.

`ops.resolve_recovery_orphan` takes `annotations=_docmeta.destructive()` and is added to
`_docmeta.DESTRUCTIVE_TOOLS`, because its promoted behavior deletes or adopts quarantined recovery
objects. The other two take `annotations=_docmeta.mutating()`.

### Wrapper contracts

Each wrapper docstring opens by saying what the tool does **today** — validates authorization and
admissibility, then reports that the recovery executor is unavailable — before describing the
operation it will perform once promoted. An agent that reads only the first sentence must not come
away believing the operation happened.

Each then states the required role, the admissible activation state, and the recovery action for the
state it cannot act on (`systems.teardown` for a System stuck in `recovery_conflict` or
`recovery_failed`; `runs.get` to observe). Each names #2118 as the promotion.

**No wrapper states a limit**, so `AGENTS.md`'s five-part limit contract (unit, reference clock,
scope, consequence, recovery action) has nothing to attach to: there is no deadline, no retry
budget, and no attempt scope under the amendment. Do not write a time contract for a time bound that
does not exist — that is the phantom the amendment removed. Task 4 step 1 asserts the absence
directly so a later edit cannot reintroduce it silently.

`Field` descriptions, one line each, no newlines (the generator rejects a newline in a parameter
description):

- `runs.release_external_boot(run_id)`.
- `systems.resolve_external_boot_conflict(system_id, operation, observed_identity)` — `operation`'s
  description names the single accepted literal `restore-recorded-source`; `observed_identity`'s
  states that it is the composite state identity from the most recent `systems.get` and that it is
  currently validated for shape only, because the compare-and-set that consumes it lands with the
  executor.
- `ops.resolve_recovery_orphan(system_id, object_identities, disposition)` — `disposition` accepts
  `delete` or `adopt`; `object_identities` is a bounded list of quarantined recovery-object
  identities.

### Steps

1. Write `tests/mcp/lifecycle/test_external_boot_contracts.py`: build the app through the existing
   assembly helper the other `tests/mcp/lifecycle` files use, then assert each of the three tools is
   registered; its `meta["maturity"] == "partial"`; its `maturity_detail` passes
   `scripts.generate.gen_tool_reference._maturity_detail`; every parameter has a newline-free
   description; the docstring discloses `recovery_executor_unavailable` and names #2118; the
   docstring contains **no** deadline vocabulary (assert the absence of `deadline`, `server_time`,
   and `seconds`, so a reintroduced time contract fails a gate); the RBAC denial envelope for each
   role boundary; and that each tool returns `error_category == "configuration_error"` with
   `data["reason"] == "recovery_executor_unavailable"` on an admissible call. Run
   `just test-verbose tests/mcp/lifecycle/test_external_boot_contracts.py` and expect failure
   because no tool is registered.
2. Add the two lifecycle wrappers to `runs/registrar.py` and `systems/registrar.py`, matching the
   surrounding registration style (`runs.boot` at `runs/registrar.py:625` is the closest analogue).
3. Create `src/kdive/mcp/tools/ops/external_boot.py` with
   `def register(app: FastMCP, pool: AsyncConnectionPool) -> None:` — the plain pool-only shape
   `ops/queue.py:263` and `ops/tuning.py:359` use — and add
   `_pool_only_plane_registrar(ops_external_boot_tools.register)` to `build_plane_registrars` in
   `src/kdive/mcp/assembly/tool_registration.py`, beside the other `ops` registrars. Add
   `"ops.resolve_recovery_orphan"` to `_docmeta.DESTRUCTIVE_TOOLS`.
4. Re-run the command from step 1 and expect exit 0.
5. Run `just docs` to regenerate the tool reference, then `just docs-check` and
   `just doc-constants-check`; expect exit 0. If `doc-constants-check` reports a stale tool count,
   run its generator without `--check` and commit the regenerated file.
6. Add the import-closure gate to `tests/services/external_boot/test_recovery_requests.py`: walk
   `kdive.services.external_boot.recovery_requests`'s module-level names and assert none of
   `begin_recovery_attempt`, `finish_recovery_attempt`, `record_conflict`, `release_reservation`,
   `mark_cleanup_complete`, `transition`, `create`, `ExternalBootAuthorityMarkerV1`, or
   `enqueue` is reachable from it. Static enforcement of the amendment's hard rule, beside Task 3
   step 3's behavioral one. Run `just test-verbose tests/services/external_boot` and expect exit 0.
7. Run `just lint`, `just type`, and `just test-changed`; expect exit 0. Commit.

Acceptance: the three tools are registered; their FastMCP schemas disclose that the executor is
absent and state no time bound; every response is a valid `ToolResponse`; the generated reference
matches a fresh generation; and both hard-rule gates pass.

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

Rollback is `git revert` of the branch. Nothing this branch registers creates a durable row, so a
revert leaves no queued external request behind and needs no drain. The pre-amendment plan's drain
step is removed with the enqueue it described.

## Design-review dispositions (2026-09-02, `$gauntlet` pass 1, 12 findings)

Eight were `accepted-fixed` in the pre-amendment revision and remain fixed: the
`suggested_next_actions` sanitizer defect; the two missing call sites (`runs.bind`,
`control.diagnostic_sysrq`); `capture_traffic` dropped from the steps and tests; the
maturity-metadata visibility claim; the savepoint consequence of the new `conn.transaction()`
blocks; the `FORCE_CRASH` scoping argument, restated as an explicit residual; four bad interface
citations; and the missing shared activation-seeding fixture.

Four were dispositioned `blocked` on the criterion the operator amended on 2026-09-02. Each is
re-dispositioned here against the amended criterion, concern and remedy judged separately:

1. **The release transition is a one-way door into `recovering` with no exit but
   `systems.teardown`** — `accepted-fixed`. The concern was verified and remains true of the
   pre-amendment design. The remedy applied is the amendment's own: no tool commits the transition,
   so the door has no entrance. Held by two gates rather than by prose — Task 3 step 3 asserts the
   activation row is unchanged after all three tools run, and Task 4 step 6 asserts the module cannot
   reach a writing name.
2. **The authority marker cannot be constructed and the server cannot allocate authority** —
   `accepted-fixed`. Both halves were verified against the tree (`ExternalBootAuthorityMarkerV1`'s
   non-optional `provider_kind` and `authority_instance`; `allocate_external_boot_authority`'s
   `kdive_worker` role gate, revoked from `kdive_server`). The remedy is removal of the only caller:
   nothing in Task 3 builds a marker or allocates authority.
3. **`CasStatus.SUPERSEDED` conflates a stale identity, an unready reservation, and a missing row** —
   `rejected-with-evidence`, on the amended design only. The concern is accurate about
   `begin_recovery_attempt`, which this branch no longer calls; its proposed remedy ("read the
   reservation under the same lock before calling `begin_recovery_attempt`") is conditioned on a call
   that does not exist here. The concern transfers to #2118 with the CAS, and is recorded in the
   follow-up return rather than acted on: acting on it would mean writing the transition the
   amendment excludes. `resolve_conflict` does not compare `observed_identity` at all, and its
   `Field` description says so, so no caller is told a check is happening that is not.
4. **The documented `dedup_key` idempotency never fires** — `accepted-fixed` by deletion. Its
   remedy was "delete the claim rather than repair it". Under the amendment there is no enqueue, no
   `dedup_key`, and no `idempotency_key` parameter on any of the three tools, so the claim and its
   subject are both gone. Task 4 step 1 asserts the wrappers carry no time or retry vocabulary, which
   catches a reintroduction.

## Deferrals

None. No finding was dispositioned `deferred-tracked`; no `docs/debt/` record was written. Finding 3
is `rejected-with-evidence` for this branch and belongs to #2118's CAS work, which the amendment
already assigns.
