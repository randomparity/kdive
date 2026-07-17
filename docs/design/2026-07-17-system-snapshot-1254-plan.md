# Implementation plan — System snapshot / restore / list / delete (#1254)

Derived from `2026-07-17-system-snapshot-1254.md` and
[ADR-0378](../adr/0378-system-snapshot-restore.md).

- **Branch:** `feat/system-snapshot-1254` (off `origin/main`).
- **Base:** `main`.
- **Guardrails (run before every commit):** `just lint`, `just type` (whole tree), targeted
  `uv run python -m pytest <files> -q`; the doc guards for doc changes (`just docs-check`,
  `docs-links`, `docs-paths`, `adr-status-check`, `resources-docs-check`); the full `just ci`
  **plus** the individually-CI-gated recipes (`docs-check`, `config-docs-check`, `env-docs-check`,
  `resources-docs-check`, `adr-status-check`, `chart-version-check`, `lint-ansible`,
  `test-ansible`) before push. `just test` alone misses generated-doc drift and cross-cutting
  behavior tests.
- **Migration:** one forward-only migration `0071_system_snapshots.sql` (byte-immutable once
  committed, ADR-0015): create the `snapshots` table, widen `jobs_kind_check`
  (`snapshot`,`restore`,`delete_snapshot`), widen `systems_state_check` (`restoring`,`paused`).
- **New dependency:** none (libvirt-python already provides `virDomainSnapshot*` /
  `virDomainResume` / `VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA`).
- **Generated artifacts (CI-gated, regenerate + commit):** adding four tools to `exposure.py`
  changes the **RBAC tool-visibility matrix** (`just rbac-matrix` → `docs/guide/safety-and-rbac.md`,
  verified by `rbac-matrix-check` under `just test`) and the **generated tool reference**
  (`just docs` → `docs/guide/reference/`, verified by `docs-check`) and the **doc-resource
  snapshots** (`just resources-docs` → `_content/`, verified by `resources-docs-check`). Never
  hand-edit a "do not edit by hand" file — edit the source and regenerate.
- **Worker execution model:** handlers run under autocommit on a dedicated dispatch connection
  with a background heartbeat (`jobs/worker.py`; long handlers supported). Snapshot/restore/delete
  do their libvirt call via `asyncio.to_thread`; only the short state commits touch the DB, each
  under `advisory_xact_lock(conn, LockScope.SYSTEM, system_id)` inside a `conn.transaction()`.
- **TDD:** each task writes the failing test(s) first, then implements. Order 1→13; dependency
  notes per task. Tasks 1–2 are the foundation; the provider seam (3–4), persistence (5), handlers
  (6), repairs (7), state-site membership sets (8), power/debug gates + sweep guard (9),
  teardown/reprovision (10), and the MCP tools (11) build on them; each drift-causing task
  regenerates its own artifacts; 13 is docs; **12 (drift verification) runs last of the
  non-live steps — after 13** (its number is kept for reference but it executes once every
  drift-causing task, including 13, has committed); 14 is the live proof.

> **Agent-surface guardrail (applies to Tasks 11–13):** the `@app.tool` **wrapper docstring +
> `Field(description=…)`** are the only agent-facing text (FastMCP serializes nothing else). They
> must name every parameter, the returned `data` fields, the poll contract, the capability, and
> the paused-restore→resume workflow, and must **not** cite ADR/issue numbers (`test_no_adr_leak`
> fails on a leaked `ADR-`/`#NNN` in the tool schema). Put rationale in the module docstring / this
> plan, never in the wrapper.

---

## Task 1 — Domain enums: `SystemState` + `SnapshotState` + `JobKind` + `PowerAction`

**Where it fits:** Spec §"System state machine", §"Domain model", §"Persistence"; ACs 8, 10.
Foundation for every later task.

**Files:**
- `src/kdive/domain/capacity/state.py` — add `SystemState.RESTORING`, `SystemState.PAUSED`; add a
  new `SnapshotState(StrEnum)` = `CREATING`/`AVAILABLE`/`FAILED`. Extend `_TRANSITIONS`: `READY`
  successor set gains `RESTORING`; add `RESTORING → {READY, PAUSED, FAILED}`,
  `PAUSED → {READY, TORN_DOWN, FAILED}`; add the `SnapshotState` adjacency
  (`CREATING → {AVAILABLE, FAILED}`, `AVAILABLE → {FAILED}`, both `FAILED`/terminal).
- `src/kdive/domain/operations/jobs.py` — add `JobKind.SNAPSHOT`, `RESTORE`, `DELETE_SNAPSHOT`;
  add all three to `ACTIVE_JOB_KINDS` and `CONTRIBUTOR_CANCELABLE_JOB_KINDS`; add
  `PowerAction.RESUME`.

**Test first** (`tests/domain/test_state.py`, `tests/domain/test_jobs.py` or the existing state
tests):
- `can_transition` accepts `READY→RESTORING`, `RESTORING→READY|PAUSED|FAILED`,
  `PAUSED→READY|TORN_DOWN|FAILED`, and rejects e.g. `PAUSED→CRASHING`, `RESTORING→CRASHED`.
- `SnapshotState` transitions: `CREATING→AVAILABLE|FAILED` legal; `AVAILABLE→FAILED` legal;
  `AVAILABLE→CREATING`, `FAILED→*` illegal.
- `JobKind.SNAPSHOT/RESTORE/DELETE_SNAPSHOT ∈ ACTIVE_JOB_KINDS ∩ CONTRIBUTOR_CANCELABLE_JOB_KINDS`;
  none in `OPT_IN_DESTRUCTIVE_JOB_KINDS`.
- `PowerAction.RESUME` exists and is distinct.

**Acceptance:** `just type` green; the state tests green; no existing transition regressed.

**Rollback:** revert the enum additions (no persisted data yet).

---

## Task 2 — Migration `0071_system_snapshots.sql`

**Where it fits:** Spec §"Persistence / migration"; AC 10. Depends on Task 1's enum names for the
CHECK values.

**Files:**
- `src/kdive/db/schema/0071_system_snapshots.sql` — header comment `-- 0071_system_snapshots.sql —
  … (#1254 / ADR-0378)`; then: `CREATE TABLE snapshots` (`id uuid PK`, `system_id uuid NOT NULL
  REFERENCES systems(id) ON DELETE CASCADE`, `name text NOT NULL`, `include_memory boolean NOT
  NULL`, `state text NOT NULL`, attribution columns `principal`/`agent_session`/`project` mirroring
  `systems`, `created_at`/`updated_at timestamptz`, `UNIQUE (system_id, name)`,
  `CONSTRAINT snapshots_state_check CHECK (state IN ('creating','available','failed'))`), the
  `snapshots_set_updated_at` trigger (copy the `systems` trigger pattern); drop-and-recreate
  `jobs_kind_check` adding `'snapshot'`,`'restore'`,`'delete_snapshot'` (keep the constraint name);
  drop-and-recreate `systems_state_check` adding `'restoring'`,`'paused'` (keep the name).

**Test first** (`tests/db/test_migrate.py` + the schema-vs-enum guard):
- The migration applies cleanly on a fresh disposable Postgres (testcontainers) and is idempotent.
- The SQL↔enum guard (the test that ties `jobs_kind_check` / `systems_state_check` values to the
  Python enums) passes with the new values — every `JobKind`/`SystemState` value is permitted by
  the constraint and vice-versa.
- Inserting two `snapshots` rows with the same `(system_id, name)` violates the UNIQUE constraint;
  deleting the `systems` row cascades the `snapshots` rows.

**Acceptance:** `uv run python -m pytest tests/db/test_migrate.py -q` green; `just type` green.

**Commit boundary:** Task 1's enum additions and this migration **land in a single commit**. The
SQL↔enum guard is bidirectional (every enum value must be permitted by the constraint and
vice-versa), so a commit that adds `SystemState.RESTORING`/`PAUSED` or the new `JobKind`s without
widening the CHECK constraints is red — splitting T1 and T2 into separate commits would leave the
T1 commit failing the guard and break `git bisect`'s per-commit-green invariant. Author them as two
TDD steps if convenient, but commit them together.

**Rollback:** the migration is byte-immutable once committed — if wrong before commit, edit;
after commit, a follow-up migration corrects it (do not edit an applied file).

---

## Task 3 — `Snapshotter` port + `ProviderSupport.supports_snapshots`

**Where it fits:** Spec §"Provider seam"; AC 6. Depends on nothing in this feature.

**Files:**
- `src/kdive/providers/core/runtime.py` — add `supports_snapshots: bool = False` to
  `ProviderSupport` (fail-closed default); add an optional `snapshot: Snapshotter | None = None`
  group to `ProviderRuntime`.
- `src/kdive/providers/ports/lifecycle.py` — add the `Snapshotter(Protocol)` with `create`,
  `revert`, `delete`, `delete_all` (signatures per spec), each documenting its `CategorizedError`
  mapping (`INFRASTRUCTURE_FAILURE` for libvirt faults; `CONFIGURATION_ERROR` for a missing
  snapshot on revert; `delete`/`delete_all` idempotent).

**Test first** (`tests/providers/test_runtime.py` or equivalent):
- A default `ProviderSupport()` has `supports_snapshots is False`; a `ProviderRuntime` with
  `snapshot=None` is valid.
- `Snapshotter` is a runtime-checkable Protocol (or structurally satisfied by a fake) — a fake with
  the four methods is accepted.

**Acceptance:** `just type` green; existing runtime tests green.

**Rollback:** revert; the field defaults keep every existing provider unchanged.

---

## Task 4 — `LocalLibvirtSnapshotter` + composition wiring

**Where it fits:** Spec §"The `Snapshotter` port"; ACs 1, 2, 4, 7. Depends on Task 3.

**Files:**
- `src/kdive/providers/local_libvirt/lifecycle/snapshot.py` — `LocalLibvirtSnapshotter` mirroring
  `LocalLibvirtControl`: a `connect: Callable[[], _LibvirtConn]` factory and a narrow
  `_LibvirtDomain` Protocol with `snapshotCreateXML` / `revertToSnapshot` / `snapshotLookupByName`
  / `listAllSnapshots`. `create` pre-deletes any same-name snapshot (defensive), then builds XML
  with `<memory snapshot='internal'/>` + internal disk (memory) or passes
  `VIR_DOMAIN_SNAPSHOT_CREATE_DISK_ONLY` (disk-only). `revert` passes
  `VIR_DOMAIN_SNAPSHOT_REVERT_RUNNING` or `..._REVERT_PAUSED`. `delete`/`delete_all` idempotent
  (swallow `VIR_ERR_NO_DOMAIN_SNAPSHOT`). Map libvirt errors to `CategorizedError` via the
  provider's `_infra` helper.
- `src/kdive/providers/local_libvirt/composition.py` — set `supports_snapshots=True` in the
  `ProviderSupport(...)` block; wire `snapshot=LocalLibvirtSnapshotter.from_env(...)`.

**Test first** (`tests/providers/local_libvirt/test_snapshot.py`, fakes for `_LibvirtConn`/domain):
- `create(include_memory=True)` calls `snapshotCreateXML` with memory-inclusive XML and no
  DISK_ONLY flag; `include_memory=False` passes `VIR_DOMAIN_SNAPSHOT_CREATE_DISK_ONLY`.
- `create` pre-deletes an existing same-name snapshot before creating.
- `revert(start_paused=True)` passes `..._REVERT_PAUSED`; `start_paused=False` passes
  `..._REVERT_RUNNING`.
- `revert` on a missing snapshot raises a `CONFIGURATION_ERROR`-categorized error; a libvirt fault
  on `create` raises `INFRASTRUCTURE_FAILURE`.
- `delete`/`delete_all` are no-ops when the snapshot/domain is absent.
- `composition.build_runtime()` exposes `support.supports_snapshots is True` and a non-None
  `snapshot` group.

**Acceptance:** `uv run python -m pytest tests/providers/local_libvirt/test_snapshot.py -q` green;
`just type` green (the libvirt C-ext import keeps its scoped `unresolved-import` ignore).

**Rollback:** revert the file + the composition lines; `supports_snapshots` returns to `False`.

---

## Task 5 — `Snapshot` model + `SNAPSHOTS` repository + queries

**Where it fits:** Spec §"Domain model"; ACs 1, 5. Depends on Tasks 1–2.

**Files:**
- `src/kdive/domain/lifecycle/records.py` — a `Snapshot(DomainModel, Attribution)` dataclass
  (`system_id: UUID`, `name: str`, `include_memory: bool`, `state: SnapshotState`).
- `src/kdive/db/repositories.py` — `SNAPSHOTS = StatefulRepository(Snapshot, "snapshots",
  SnapshotState, ...)`; add query helpers: `insert` a `creating` row (respecting `UNIQUE`),
  `get_by_name(conn, system_id, name)`, `list_for_system(conn, system_id)` newest-first,
  `delete_row(conn, id)`, and an existing-row classifier for admission (available/creating/failed).

**Test first** (`tests/db/test_repositories.py` or a new `test_snapshots_repo.py`, disposable PG):
- Insert→`get_by_name` round-trips; `list_for_system` returns newest-first.
- `update_state` enforces the `SnapshotState` machine (illegal edge → `IllegalTransition`).
- A second insert of the same `(system_id, name)` raises the UNIQUE violation (surfaced as a typed
  admission error in Task 11, but the repo raises here).
- `delete_row` removes the row; cascade removal on `systems` delete (from Task 2) still holds.

**Acceptance:** repo tests green; `just type` green.

**Rollback:** revert model + repository additions.

---

## Task 6 — Job payloads + handlers (`snapshot`/`restore`/`delete_snapshot`) + registration

**Where it fits:** Spec §"systems.snapshot/restore/delete_snapshot" worker halves; ACs 1, 3, 3b,
4, 5, 8. Depends on Tasks 1–5.

**Files:**
- `src/kdive/jobs/payloads.py` — `SnapshotPayload(SystemPayload)` (`+snapshot_id, name,
  include_memory`), `RestorePayload(SystemPayload)` (`+name, start_paused`),
  `SnapshotDeletePayload(SystemPayload)` (`+name`); register each in the kind→model dispatch map.
- `src/kdive/jobs/handlers/systems.py` (or a new `handlers/snapshots.py` imported by the systems
  registrar) — `snapshot_handler`: re-verify `READY`, `runtime.snapshot.create(...)` off-thread,
  commit the `snapshots` row `creating→available` (or `→failed`) under the SYSTEM lock; never
  touch the System row. `restore_handler`: re-verify `RESTORING`, `runtime.snapshot.revert(...)`
  off-thread, commit `RESTORING→READY` (running) or `RESTORING→PAUSED` (start_paused), or
  `RESTORING→FAILED` on error/cancel — all under the SYSTEM lock, committing the System transition
  **before returning** (so the framework marks the job terminal only after). `snapshot_delete_handler`:
  `runtime.snapshot.delete(...)` off-thread, then `SNAPSHOTS.delete_row(...)` under the lock.
- Register all three in `register_handlers(...)` (`registry.register(JobKind.SNAPSHOT, …)` etc.).

**Test first** (`tests/jobs/handlers/test_snapshots.py`, a fake `Snapshotter` + in-memory/PG repo):
- `snapshot_handler` success drives the row `creating→available`, leaves System `READY`; a
  provider error drives `creating→failed`, System `READY`.
- `restore_handler` success (running) drives `RESTORING→READY`; `start_paused` drives
  `RESTORING→PAUSED`; a provider error / simulated cancel drives `RESTORING→FAILED` and never
  `READY`.
- The restore handler commits the System transition before returning (assert ordering via a
  handler that records commit-vs-return, or that a terminal-job repair pass — Task 7 — cannot see
  terminal+RESTORING on the success path).
- `snapshot_delete_handler` removes the row after the libvirt delete; a delete on an already-gone
  snapshot still removes the row (idempotent).

**Acceptance:** handler tests green; `just type` green.

**Rollback:** revert handlers + payloads + registration; the kinds remain unused.

---

## Task 7 — Reconciler repairs: `repair_stalled_restoring_systems` + `repair_stalled_creating_snapshots`

**Where it fits:** Spec §"Concurrency… Stuck-transition recovery" and "Stranded `creating`
recovery"; AC 8. Depends on Tasks 1–2, 5–6.

**Files:**
- `src/kdive/reconciler/repairs/systems.py` — `repair_stalled_restoring_systems`: a System in
  `RESTORING` with **no active `RESTORE` job** → `FAILED`, under the SYSTEM lock, re-reading state
  under the lock (mirror `repair_stalled_crashing_systems`).
- `src/kdive/reconciler/repairs/snapshots.py` (new, or beside systems) —
  `repair_stalled_creating_snapshots`: a `snapshots` row in `creating` whose `SNAPSHOT` job is
  terminal/absent → `failed`.
- Register both in the reconciler repair catalog (wherever `repair_stalled_crashing_systems` is
  scheduled).

**Test first** (`tests/reconciler/test_repairs.py`, disposable PG):
- A `RESTORING` System with a terminal/absent `RESTORE` job → `FAILED`; with a **live** `RESTORE`
  job → untouched.
- The repair does **not** clobber a restore that already committed `READY`/`PAUSED` (seed that
  state + a just-terminal job; assert no transition).
- A `creating` snapshot with a terminal/absent `SNAPSHOT` job → `failed`; with a live job →
  untouched.

**Acceptance:** repair tests green; `just type` green.

**Rollback:** revert the repairs + their registration.

---

## Task 8 — State-exhaustive-site membership-set updates

**Where it fits:** Spec §"System state machine" enumeration; AC 9. Depends on Task 1. **The
discovery-sweep guard is authored in Task 9**, after Task 9 widens the debug/power *gates* — so the
sweep is written against the final state of every site and there is no ordering cycle (this task
updates only the membership *sets*, which have no dependency on the gates).

**Files:**
- `src/kdive/reconciler/repairs/allocations.py` — add `RESTORING`, `PAUSED` to
  `_NON_TERMINAL_SYSTEM`.
- `src/kdive/services/systems/admission.py` — add `RESTORING`, `PAUSED` to the non-terminal
  (quota-holding) set; keep new-Run admission requiring `READY` (do **not** list PAUSED/RESTORING
  as launchable).
- `src/kdive/providers/infra/console_hosting.py` — add `RESTORING`, `PAUSED` to the live-state set.
- `src/kdive/jobs/handlers/console/console_rotate.py` — add `RESTORING`, `PAUSED` to `_LIVE_STATES`.

**Test first:** behavioral tests per site — a `RESTORING`/`PAUSED` System is counted non-terminal
by allocation reaping + quota admission, its console is hosted and sealed; new-Run admission still
refuses `RESTORING`/`PAUSED`. Watch them fail against the un-updated sets, then update.

**Acceptance:** the per-site tests green; `just test` (reconciler/admission/console) green;
`just type` green.

**Rollback:** revert the set additions.

---

## Task 9 — `control.power` `RESUME` gate + `debug.start_session` `PAUSED` gate + discovery-sweep guard

**Where it fits:** Spec §"Paused restore & resume", §enumeration (power + debug gates); ACs 4, 9.
Depends on Tasks 1, 8. This task widens the last two state-keyed sites (the debug + power gates)
and then authors the sweep guard, so the sweep sees every site in its final form (closing the
Task-8/9 cycle).

**Files:**
- `src/kdive/mcp/tools/lifecycle/control/registrar.py` (`power_system`) — admit `RESUME` iff the
  System is `PAUSED` (else `configuration_error`); keep every other action requiring `READY`
  (so `RESUME` from `READY` and ON/OFF/CYCLE/RESET from `PAUSED`/`RESTORING` are refused).
- `src/kdive/jobs/handlers/control/control.py` (`_power_target` / `power_handler`) — accept a
  `PAUSED` target for `RESUME` only (`_power_target` today raises "power requires a READY system"
  for any non-`READY`); the `RESUME` path **commits `PAUSED→READY`** under the SYSTEM lock (a
  documented exception to the handler's move-no-state rule), and a failed resume routes
  `PAUSED→FAILED`. Every other action still moves no state and requires `READY`. **This file does
  the DB state commit and delegates the libvirt call to the provider** (`asyncio.to_thread(
  control.power, domain_name, action)`) — it does not itself call libvirt.
- **`src/kdive/providers/local_libvirt/lifecycle/control.py` — the actual `virDomainResume`
  dispatch.** `_apply_power` (not the job handler) is where the libvirt power call lives. Add an
  explicit `elif action is PowerAction.RESUME: domain.resume()` branch and extend the narrow
  `_LibvirtDomain` Protocol with `resume()` (→ `virDomainResume`). **Convert the current tail
  `else: # PowerAction.CYCLE` into an explicit `elif action is PowerAction.CYCLE` + a final
  `assert_never(action)`** so a new power action can never silently fall through to `reboot(0)` —
  today `RESUME` would otherwise hit the `else` and *reboot* the guest, destroying the paused
  state (the opposite of resume) with no type error and no fake-provider unit-test failure.
- `src/kdive/mcp/tools/debug/sessions/lifecycle.py` — widen the `start_session` gate from
  `state is READY` to `state in {READY, PAUSED}`.
- **`src/kdive/mcp/tools/lifecycle/control/registrar.py` — the `control.power` `@app.tool`
  *wrapper* `Field(description=…)` and docstring (the `action` param is a `str`, so the wrapper
  text is the ONLY agent-facing surface; editing it is what drifts the generated reference — an
  enum member alone changes no schema).** Add `resume` to the action list, state it is admitted
  **only from `PAUSED`** and commits `PAUSED→READY`, name the `start_paused` restore → `resume`
  workflow, and **correct the now-false "Admitted only on a READY System" / "Refused on a
  non-READY System" wording** (resume is the one action admitted from a non-`READY` state). This is
  distinct from the `power_system` admission helper edited above. **Agent-surface guardrail
  (extends the preamble note to this task):** no `ADR-`/`#NNN` in the wrapper text.
- `tests/domain/test_state_site_coverage.py` (new) — the **discovery sweep**. It must be a
  **whole-tree AST scan** (glob `src/kdive/**/*.py`, `ast`-parse each), **not** introspection over
  a hand-picked module list — a fixed list is the exact failure mode the sweep exists to prevent
  (it already missed the power + console-rotate gates). Follow the repo's established whole-tree
  scan precedent (`tests/cli/test_no_service_import.py`, `tests/providers/test_provider_boundaries.py`).
  Discover every `frozenset[SystemState]` literal / `SystemState`-membership set and `state is …
  READY` gate anywhere in the tree and assert each `SystemState` value is either present or on an
  explicit `INTENTIONALLY_EXCLUDED` allow-list with a reason — so a state-keyed site added later in
  *any* module fails the guard. Seed the allow-list with the deliberate exclusions (new-Run
  admission excludes `PAUSED`/`RESTORING`; the `debug` gate excludes `RESTORING`; non-`RESUME`
  power actions exclude `PAUSED`/`RESTORING`).
- **Regenerate the generated tool reference** (`just docs` → `docs/guide/reference/`) in this
  commit: editing the `control.power` wrapper `Field`/docstring text (above) drifts the reference
  (the reference is generated from the wrapper text, not the enum); regenerate + commit so this
  commit passes `docs-check`.

**Test first** (`tests/mcp/.../test_control_power.py`, `tests/mcp/.../test_debug_sessions.py`, the
sweep test):
- `control.power(action=resume)` admitted from `PAUSED`, refused from `READY`/`RESTORING`/others.
- ON/OFF/CYCLE/RESET refused from `PAUSED`/`RESTORING`.
- The `RESUME` handler commits `PAUSED→READY` on success and `PAUSED→FAILED` on a simulated
  `virDomainResume` failure.
- **Provider-level:** `LocalLibvirtControl._apply_power(PowerAction.RESUME)` calls
  `domain.resume()` and **not** `domain.reboot()` (a fake `_LibvirtDomain` asserting which method
  fired) — catches a forgotten branch / catch-all fall-through at unit-test time, not only at the
  Task 14 live proof.
- `debug.start_session` succeeds against a `PAUSED` System and (regression) still against `READY`;
  refused against non-`READY`/non-`PAUSED`.
- The sweep asserts `RESTORING`/`PAUSED` coverage across all state-keyed sites; watch it fail if a
  gate is left un-widened, then green.
- **The `control.power` wrapper `Field`/docstring text mentions `resume`** (and no longer claims
  power is refused on every non-`READY` state) — a schema/text assertion so the doc edit is
  verified, not assumed.

**Acceptance:** the power + debug + sweep tests green; the wrapper-text assertion green; `just type`
and `just docs-check` green.

**Rollback:** revert the gate widenings + the sweep test; `RESUME` becomes unreachable (Task 1's
enum value is inert).

---

## Task 10 — Teardown + reprovision snapshot-awareness

**Where it fits:** Spec §"Teardown", §"Reprovision invalidates snapshots"; ACs 7, 7b. Depends on
Tasks 4–6.

**Files:**
- `src/kdive/providers/local_libvirt/lifecycle/provisioning.py` — pass
  `VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA` in the **shared** `_teardown_domain` undefine call (so
  both teardown and reprovision succeed on a snapshotted domain).
- `src/kdive/jobs/handlers/systems.py` — `teardown_handler`: call `runtime.snapshot.delete_all(...)`
  before undefine and delete the System's `snapshots` rows in the reclaim step (cascade also
  covers a row delete). `reprovision_handler` (or its commit): delete the System's `snapshots`
  ledger rows as part of the reprovision commit (the recreated qcow2 destroys the libvirt
  snapshots).

**Test first:**
- `teardown_handler` on a snapshotted System calls `delete_all`, undefines with the metadata flag,
  and leaves no `snapshots` rows (including a `creating`/`failed` orphan).
- `reprovision_handler` deletes the System's `snapshots` rows; a later `get_by_name` finds none,
  so a subsequent restore of an old name is refused (`configuration_error`, not `FAILED`).
- The undefine-with-flag is exercised so a snapshotted-domain undefine does not raise.

**Acceptance:** teardown/reprovision handler tests green; `just type` green.

**Rollback:** revert; teardown/reprovision return to snapshot-unaware (only safe if the feature is
fully reverted).

---

## Task 11 — MCP tools + `exposure.py` + `systems.get` capability

**Where it fits:** Spec §"Tools"; ACs 1–6. Depends on Tasks 3–9. **Agent-surface guardrail applies.**

**Files:**
- `src/kdive/mcp/tools/lifecycle/systems/registrar.py` — four new `_register_systems_*` calls
  inside `register()`: `systems.snapshot` (`mutating`, contributor, enqueue `SNAPSHOT`),
  `systems.restore` (`mutating`, contributor, admission per spec: `available` snapshot, mode
  validation, reject live Run (`_has_live_run`) / active `SNAPSHOT`|`RESTORE`|`DELETE_SNAPSHOT`
  job (job-queue query on `system_id`) / **attached debug session** (query `DEBUG_SESSIONS`
  joined `debug_sessions.run_id → runs.id` where `runs.system_id = :sid` and
  `debug_sessions.state != DETACHED` — i.e. state in `{ATTACH, LIVE}`; refuse
  `configuration_error` "end the debug session first"), transition `READY→RESTORING`, enqueue
  `RESTORE`), `systems.list_snapshots` (`read_only`,
  viewer, `ToolResponse.collection`), `systems.delete_snapshot` (`mutating`, contributor,
  Postgres-only admission, reject `creating`/`RESTORING`/in-flight delete, enqueue
  `DELETE_SNAPSHOT` with `recycle_terminal=True, recycle_canceled=True`). Wrapper docstrings +
  `Field` text carry the full agent-facing contract (params, poll, capability, paused→resume
  workflow, disk-only reboot + memory-pause caveats). Admission handlers on `SystemAdminHandlers`
  (or a new `SystemSnapshotHandlers`).
- `src/kdive/mcp/tools/lifecycle/systems/…get…` — add `data.supports_snapshots` to `systems.get`
  (resolve `runtime_for_system`, read `runtime.support.supports_snapshots`; no libvirt call).
- `src/kdive/security/authz/exposure.py` — register the four tools' RBAC exposure
  (`snapshot`/`restore`/`delete_snapshot` = `_CONTRIBUTOR`, `list_snapshots` = `_VIEWER`).
- **Regenerate in this commit** (`just docs` → tool reference, `just rbac-matrix` →
  `docs/guide/safety-and-rbac.md`): the four new tools drift both; regenerate + commit so this
  commit passes `docs-check` and the `rbac-matrix` guard (`just test`). If the systems guide is a
  mirrored doc-resource, defer its `resources-docs` regen to Task 13 (which edits it).

**Test first** (`tests/mcp/lifecycle/test_systems_snapshot.py`, handler-level, injected pool +
`RequestContext`, fake runtime):
- `systems.snapshot` on `READY` (with and without a live Run) inserts `creating` + enqueues
  `SNAPSHOT`, returns `{job_id, status: queued}`; name-collision rules (reject `available`, recycle
  `failed`, replay live `creating`, re-create stale-`creating`).
- `systems.restore` happy path + each refusal (non-`available` snapshot, disk-only+start_paused,
  live Run, active `SNAPSHOT`/`RESTORE`/`DELETE_SNAPSHOT` job, attached debug session) →
  `configuration_error`.
- `systems.list_snapshots` returns a newest-first collection with no libvirt call; empty for a
  supported provider with none.
- `systems.delete_snapshot` enqueues `DELETE_SNAPSHOT`; refused for `creating`/`RESTORING`.
- All four return `capability_unsupported` on a provider with `supports_snapshots is False`;
  `systems.get` surfaces `data.supports_snapshots` for both provider kinds without a libvirt call.
- RBAC: a viewer is denied the mutating tools; an ungranted project is indistinguishable from
  absent.

**Acceptance:** tool tests green; `just type` green; `test_no_adr_leak` green (no `ADR-`/`#NNN` in
the wrapper schemas); `just docs-check` and the `rbac-matrix` guard green (regenerated in this
commit).

**Rollback:** revert the registrar/exposure/get additions; the handlers/jobs become unreachable.

---

## Task 12 — Verify no generated-artifact drift remains

**Where it fits:** plan preamble "Generated artifacts"; gates `docs-check`, `rbac-matrix-check`,
`resources-docs-check`. Depends on Tasks 9, 11, 13 — so despite its number, **this pass executes
after Task 13's docs commit** (it is the last non-live step).

The drift-causing regenerations happen **inside the task that causes them** so each commit is
self-consistent and bisectable: the tool reference is regenerated in Task 9 (the `control.power`
wrapper edit) and Task 11 (four tools), the RBAC matrix in Task 11, and the doc-resource snapshots
in Task 13 (if the systems guide is mirrored). This task is the final catch-all check that nothing
was missed.

**Steps:** run `just docs-check`, `just config-docs-check`, `just resources-docs-check`,
`just env-docs-check`, and the `rbac-matrix` guard (via `just test`). If any reports drift, a prior
task's in-commit regen was incomplete — regenerate the source and fold the fixup into that task's
commit (not a new trailing commit) where practical.

**Acceptance:** all generated-doc guards green with a clean working tree (`git status` empty after
regeneration).

**Rollback:** n/a (verification).

---

## Task 13 — Docs: `systems` toolset guide + agent index

**Where it fits:** Spec AC 11. Depends on Task 11. **Agent-surface guardrail applies to any
wrapper text touched.**

**Files:**
- `docs/guide/toolsets/systems.md` (or the systems toolset guide) — document the four tools, the
  `supports_snapshots` capability, the paused-restore→`control.power(resume)` workflow, the
  disk-only-reboot + memory-pause caveats, and the "freed on release" contract.
- the agent index / Observe/Act step doc — list the snapshot tools where reprovision/power are.

**Test first:** `just docs-links`, `just docs-paths` green after editing (they gate broken
links/paths). If the guide is a mirrored doc-resource, re-run `just resources-docs` (Task 12).

**Acceptance:** doc guards green; the guide describes the workflow an agent follows end-to-end.

**Rollback:** revert the doc edits.

---

## Task 14 — Live proof (`live_vm`, this host runs KVM/libvirt)

**Where it fits:** functional validation that the libvirt snapshot/revert/resume path actually
works (this dev host runs `live_vm` tests directly). Depends on all prior tasks.

**Steps (manual / a `live_vm`-marked test if feasible):**
- Provision a local System; `systems.snapshot(include_memory=True)`; verify `available`.
- Trigger a change in the guest; `systems.restore` (running) and confirm the change is rolled back.
- `systems.restore(start_paused=True)` → System `PAUSED`; `debug.start_session` attaches;
  `control.power(action=resume)` → `READY` and the guest runs.
- `systems.delete_snapshot` frees the name; `systems.reprovision` on a snapshotted System does not
  fail at undefine and leaves no stale `available` rows; teardown leaves no `snapshots` rows.

**Acceptance:** the live loop succeeds end-to-end; record the proof in the PR description. If a
`live_vm` test is added, it is marked and skips cleanly without a host.

**Rollback:** n/a (validation only).

---

## Commit sequence

One logical change per commit, imperative ≤72-char subjects, `Co-Authored-By` trailer. Each commit
must be green on its own (per-commit-green bisect invariant): regenerate any drifted generated
artifact **within** the commit that drifts it (Tasks 9, 11, 13), never in a trailing batch.
Suggested:
`feat(1254): snapshot domain enums + migration 0071` (T1+T2, one commit — see Task 2 commit
boundary) · `feat(1254): Snapshotter port + supports_snapshots capability` (T3) · `feat(1254):
local-libvirt snapshotter` (T4) · `feat(1254): snapshots repository + model` (T5) · `feat(1254):
snapshot/restore/delete job handlers` (T6) · `feat(1254): reconciler repairs for stranded
restore/snapshot` (T7) · `feat(1254): state-site membership-set updates` (T8) · `feat(1254):
control.power resume + debug PAUSED gate + state-site sweep` (T9, regenerates the tool reference) ·
`feat(1254): teardown/reprovision free snapshots` (T10) · `feat(1254): systems
snapshot/restore/list/delete tools` (T11, regenerates tool reference + RBAC matrix) · `docs(1254):
systems snapshot guide` (T13, regenerates doc-resource snapshots if mirrored). Task 12 is a
verification pass, not a commit. Run the guardrails before each commit; the full `just ci` + doc
guards before push.
