# force_crash `crashing` state (#1078) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the residual `force_crash` physical-crash-window race by introducing a durable transient `crashing` System state that the power path's existing non-`READY` guard auto-rejects, plus reconciler leak-recovery.

**Architecture:** `force_crash` transitions `ready → crashing` under the `SYSTEM` lock *before* firing the unlocked NMI, then `crashing → crashed`. The power path is unchanged (its non-`READY` refusal covers `crashing`). A reconciler repair recovers a `crashing` System whose `force_crash` job is no longer active → `crashed` (evidence-first).

**Tech Stack:** Python 3.14, `uv`, `ruff`, `ty`, `pytest`; Postgres (advisory locks, forward-only SQL migrations under `src/kdive/db/schema/`); psycopg async.

**Spec:** `docs/superpowers/specs/2026-07-10-force-crash-crashing-marker-1078-design.md`
**ADR:** `docs/adr/0325-force-crash-crashing-state.md`

## Global Constraints

- Branch: `feat/force-crash-nmi-race-1078` off `main`. Never commit on `main`.
- Guardrails (run before each commit): `just lint`, `just type` (whole tree), and the touched tests. Full gate before push: `just ci`. Single test: `uv run python -m pytest <path>::<name> -q`.
- DB/handler/reconciler tests need Docker (disposable Postgres via testcontainers); they use the `migrated_url` fixture. They skip locally when Docker is absent — run them where Docker is available before push.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` strict. Google-style docstrings on non-trivial public APIs. Absolute imports only.
- Doc prose guard: use "Milestone" not "Sprint"; avoid "critical/robust/comprehensive/elegant".
- Migration number is **0065** (next free); it is forward-only and additive.
- Every commit message ends with the trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Stage explicit paths only — never `git add -A`.

---

### Task 1: `CRASHING` state, transition table, migration 0065

**Files:**
- Modify: `src/kdive/domain/capacity/state.py` (enum ~line 70; `SystemState` adjacency ~line 162-181)
- Modify: `tests/domain/test_state.py` (mirror adjacency ~line 58-78)
- Create: `src/kdive/db/schema/0065_system_crashing_state.sql`

**Interfaces:**
- Produces: `SystemState.CRASHING = "crashing"`. New legal edges: `READY→CRASHING`, `CRASHING→{CRASHED,FAILED,TORN_DOWN}`. Removed edge: `READY→CRASHED` (dead — `force_crash` was its only producer). `can_transition(frm, to)` reflects these.

- [ ] **Step 1: Write the failing transition tests**

In `tests/domain/test_state.py`, first update the mirror adjacency table (it is asserted equal to the production table by an existing test). In the `SystemState:` block, change the `READY` set and add a `CRASHING` entry:

```python
        SystemState.READY: {
            SystemState.CRASHING,
            SystemState.TORN_DOWN,
            SystemState.REPROVISIONING,
            SystemState.FAILED,
        },
        SystemState.CRASHING: {
            SystemState.CRASHED,
            SystemState.FAILED,
            SystemState.TORN_DOWN,
        },
        SystemState.CRASHED: {SystemState.TORN_DOWN, SystemState.FAILED},
```

Then **update the existing** `test_force_crash_edge_is_legal_teardown_skip_is_not` (currently at `tests/domain/test_state.py:214-219`) — it asserts the now-removed direct edge. Replace its body:

```python
def test_force_crash_edge_is_legal_teardown_skip_is_not() -> None:
    # The force_crash path is now two-step: ready -> crashing -> crashed -> torn_down (#1078).
    assert can_transition(SystemState.READY, SystemState.CRASHING) is True
    assert can_transition(SystemState.CRASHING, SystemState.CRASHED) is True
    assert can_transition(SystemState.CRASHED, SystemState.TORN_DOWN) is True
    # The direct ready -> crashed edge is removed: force_crash goes through crashing.
    assert can_transition(SystemState.READY, SystemState.CRASHED) is False
    # A System cannot un-crash back to ready.
    assert can_transition(SystemState.CRASHED, SystemState.READY) is False
```

Then add explicit new edge tests (place near the other `SystemState` transition tests):

```python
def test_system_crashing_edges() -> None:
    assert can_transition(SystemState.CRASHING, SystemState.FAILED)
    assert can_transition(SystemState.CRASHING, SystemState.TORN_DOWN)
    # crashing cannot go back to ready (evidence-first; recovery is to crashed).
    assert not can_transition(SystemState.CRASHING, SystemState.READY)
```

Also grep the file for any other `READY`/`CRASHED` adjacency assertion and reconcile it with the new table:

```bash
rg -n "READY.*CRASHED|CRASHED.*READY" tests/domain/test_state.py
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/domain/test_state.py -q`
Expected: FAIL — `AttributeError: CRASHING` (enum missing) and/or the mirror-equality test failing.

- [ ] **Step 3: Add the enum value**

In `src/kdive/domain/capacity/state.py`, add to `SystemState` (after `READY`, mirroring lifecycle order — place it between `REPROVISIONING` and `CRASHED` is fine; order is cosmetic):

```python
    READY = "ready"
    REPROVISIONING = "reprovisioning"
    CRASHING = "crashing"
    CRASHED = "crashed"
```

Update the `SystemState` docstring to mention the transient: add a sentence like "`force_crash` cycles a ready System `ready → crashing → crashed`; the `crashing` marker is set before the physical NMI so the power path refuses it (ADR-0325)."

- [ ] **Step 4: Edit the production transition table**

In the same file's adjacency table (`_TRANSITIONS` / the `SystemState:` mapping ~line 162):

```python
        SystemState.READY: frozenset(
            {
                SystemState.CRASHING,
                SystemState.TORN_DOWN,
                SystemState.REPROVISIONING,
                SystemState.FAILED,
            }
        ),
        SystemState.REPROVISIONING: frozenset({SystemState.READY, SystemState.FAILED}),
        SystemState.CRASHING: frozenset(
            {SystemState.CRASHED, SystemState.FAILED, SystemState.TORN_DOWN}
        ),
        SystemState.CRASHED: frozenset({SystemState.TORN_DOWN, SystemState.FAILED}),
```

(Remove `SystemState.CRASHED` from the `READY` frozenset; add the new `CRASHING` frozenset.)

- [ ] **Step 5: Write the migration**

Create `src/kdive/db/schema/0065_system_crashing_state.sql`:

```sql
-- 0065_system_crashing_state.sql — add the transient `crashing` System state (ADR-0325, #1078).
-- Forward-only (ADR-0015), additive: widens the CHECK to allow 'crashing', the pre-NMI marker
-- force_crash sets before firing the physical NMI so the power path's non-READY guard refuses it.
-- No existing row is 'crashing', so there is no data backfill.
ALTER TABLE systems DROP CONSTRAINT systems_state_check;
ALTER TABLE systems ADD CONSTRAINT systems_state_check
    CHECK (state IN ('defined', 'provisioning', 'ready', 'reprovisioning',
                     'crashing', 'crashed', 'torn_down', 'failed'));
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run python -m pytest tests/domain/test_state.py -q`
Expected: PASS (including the pre-existing mirror-equality test).

- [ ] **Step 7: Lint + type**

Run: `just lint && just type`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add src/kdive/domain/capacity/state.py tests/domain/test_state.py src/kdive/db/schema/0065_system_crashing_state.sql
git commit -m "feat(state): add transient CRASHING System state + migration 0065 (#1078)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: State-set fan-out (quota, allocation reaper, console)

**Files:**
- Modify: `src/kdive/services/systems/admission.py` (`_NON_TERMINAL_SYSTEM` ~line 58)
- Modify: `src/kdive/reconciler/repairs/allocations.py` (`_LIVE_SYSTEM_STATES` ~line 50)
- Modify: `src/kdive/providers/infra/console_hosting.py` (`_RUNNING_SYSTEM_STATE_VALUES` ~line 26)
- Modify: `src/kdive/jobs/handlers/console_rotate.py` (`_LIVE_STATES` ~line 67)
- Modify: `src/kdive/reconciler/repairs/console_rotation.py` (`_LIVE_SYSTEM_STATES` ~line 33)
- Test: `tests/domain/test_system_state_sets.py` (create — a focused guard that these live sets include `CRASHING`)

**Interfaces:**
- Consumes: `SystemState.CRASHING` (Task 1).
- Produces: `CRASHING ∈` each "live/non-terminal" set above; `CRASHING ∉` `RUN_HOSTABLE`/`SYSTEM_GONE`/terminal sets (unchanged).

- [ ] **Step 1: Write the failing guard test**

Create `tests/domain/test_system_state_sets.py`:

```python
"""CRASHING must join the live/non-terminal state sets, not the terminal/hostable ones (#1078)."""

from __future__ import annotations

from kdive.domain.capacity.state import SystemState
from kdive.domain.lifecycle.rules import TERMINAL_SYSTEM_STATES
from kdive.jobs.handlers.console_rotate import _LIVE_STATES
from kdive.providers.infra.console_hosting import _RUNNING_SYSTEM_STATE_VALUES
from kdive.reconciler.repairs.allocations import _LIVE_SYSTEM_STATES as _ALLOC_LIVE
from kdive.reconciler.repairs.console_rotation import _LIVE_SYSTEM_STATES as _ROT_LIVE
from kdive.services.runs.states import RUN_HOSTABLE, SYSTEM_GONE
from kdive.services.systems.admission import _NON_TERMINAL_SYSTEM


def test_crashing_is_live_and_non_terminal() -> None:
    assert SystemState.CRASHING in _NON_TERMINAL_SYSTEM  # occupies a quota slot
    assert SystemState.CRASHING in _ALLOC_LIVE  # allocation not orphaned mid-crash
    assert SystemState.CRASHING.value in _RUNNING_SYSTEM_STATE_VALUES  # console keeps streaming
    assert SystemState.CRASHING in _LIVE_STATES  # console rotation live
    assert SystemState.CRASHING.value in _ROT_LIVE  # reconciler console rotation live


def test_crashing_is_not_hostable_gone_or_terminal() -> None:
    assert SystemState.CRASHING not in RUN_HOSTABLE  # no new Run on a crashing System
    assert SystemState.CRASHING not in SYSTEM_GONE  # transient, not gone
    assert SystemState.CRASHING not in TERMINAL_SYSTEM_STATES
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/domain/test_system_state_sets.py -q`
Expected: FAIL — `CRASHING` absent from the live sets.

- [ ] **Step 3: Add CRASHING to the five live sets**

`src/kdive/services/systems/admission.py` — add `SystemState.CRASHING,` to `_NON_TERMINAL_SYSTEM` (after `SystemState.CRASHED`). Update the comment to note crashing also occupies a quota slot.

`src/kdive/reconciler/repairs/allocations.py` — add `SystemState.CRASHING,` to `_LIVE_SYSTEM_STATES` (after `SystemState.CRASHED`). Update the leading comment: a `crashing` System's allocation backs an in-progress crash and is live, not orphaned.

`src/kdive/providers/infra/console_hosting.py` — add `SystemState.CRASHING.value,` to `_RUNNING_SYSTEM_STATE_VALUES` (after `SystemState.CRASHED.value`).

`src/kdive/jobs/handlers/console_rotate.py` — change `_LIVE_STATES` to `frozenset({SystemState.READY, SystemState.CRASHING, SystemState.CRASHED})`.

`src/kdive/reconciler/repairs/console_rotation.py` — add `SystemState.CRASHING.value,` to `_LIVE_SYSTEM_STATES`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run python -m pytest tests/domain/test_system_state_sets.py -q`
Expected: PASS.

- [ ] **Step 5: Run the touched modules' existing tests + lint/type**

Run: `uv run python -m pytest tests/services/systems tests/reconciler/test_orphaned_active_sweep.py -q && just lint && just type`
Expected: PASS / clean. (If a testcontainers test skips for lack of Docker, note it and run where Docker is available.)

- [ ] **Step 6: Commit**

```bash
git add src/kdive/services/systems/admission.py src/kdive/reconciler/repairs/allocations.py src/kdive/providers/infra/console_hosting.py src/kdive/jobs/handlers/console_rotate.py src/kdive/reconciler/repairs/console_rotation.py tests/domain/test_system_state_sets.py
git commit -m "feat(state): treat CRASHING as live in quota, allocation-reaper, console sets (#1078)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `force_crash` handler — state-conditional, finalize-only retry

**Files:**
- Modify: `src/kdive/jobs/handlers/control.py` (`force_crash_handler` ~line 107; `_force_crash_target` ~line 124 — deleted; `_finalize_force_crash` ~line 138)
- Test: `tests/adversarial/test_provider_state_races.py` (reuse the `_RecordingController`/`_seed_system`/`_enqueue`/`_system_state` harness)
- Test: `tests/mcp/lifecycle/test_control_tools.py` (existing force_crash-handler tests — one asserts the old re-fire-on-CRASHED contract)

**Interfaces:**
- Consumes: `SystemState.CRASHING`, transitions (Task 1); `SYSTEMS.get`/`SYSTEMS.update_state`; `advisory_xact_lock(conn, LockScope.SYSTEM, system_id)`; `_controller(conn, system_id, resolver)`; `detach_sessions(conn, job, system)`; `audit.record`.
- Produces: `force_crash_handler` drives `ready → crashing → crashed`. New locked helpers: `_force_crash_precheck(conn, system_id) -> _CrashPrecheck` and `_enter_crashing(conn, system_id) -> _ControlTarget | None`. A retry with the System already `crashing` finalizes without re-firing the NMI and without resolving the controller. `control.force_crash` raising propagates (no `CRASHING → FAILED` in the handler).
- **Intentional contract change:** `force_crash` on an already-`CRASHED` System is now a no-op (`precheck → "done"`, **no NMI re-fire**), where the old handler re-fired the NMI (it treated non-terminal `CRASHED` as a fresh target). This matches the finalize-only / never-re-fire-into-a-crashed-guest rule (ADR-0325) and the audited system transition becomes `crashing->crashed` (was `ready->crashed`). Both are asserted by existing tests that must be updated (Step 6 below).

- [ ] **Step 1: Write the failing handler tests**

Add to `tests/adversarial/test_provider_state_races.py`. First a controller whose NMI can raise, near `_RecordingController`:

```python
class _RaisingCrashController(_RecordingController):
    """force_crash raises like a degraded provider; power still records."""

    def force_crash(self, domain_name: str) -> None:
        self.crashed.append(domain_name)
        raise CategorizedError("inject-nmi failed", category=ErrorCategory.INFRASTRUCTURE_FAILURE)
```

Then the tests:

```python
def test_force_crash_drives_ready_crashing_crashed(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id = await _seed_system(pool, SystemState.READY, domain_name="kdive-x")
            ctrl = _RecordingController()
            resolver = provider_resolver(provisioner=_TrackingProvisioner(), controller=ctrl)
            job = await _enqueue(pool, JobKind.FORCE_CRASH, system_id, f"{system_id}:force_crash")
            async with pool.connection() as conn:
                await control_plane.force_crash_handler(conn, job, resolver=resolver)
            assert await _system_state(pool, system_id) == SystemState.CRASHED.value
            assert ctrl.crashed == ["kdive-x"]  # NMI fired exactly once
            # Non-skippable CI guard on the system-transition audit literal (also used by the
            # gated live_stack proof and the reconciler recovery):
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log "
                    "WHERE object_kind = 'systems' AND transition = 'crashing->crashed'"
                )
                row = await cur.fetchone()
            assert row is not None and row["n"] == 1

    asyncio.run(_run())


def test_force_crash_retry_after_crashing_finalizes_without_second_nmi(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id = await _seed_system(pool, SystemState.READY, domain_name="kdive-x")
            ctrl = _RecordingController()
            resolver = provider_resolver(provisioner=_TrackingProvisioner(), controller=ctrl)
            job = await _enqueue(pool, JobKind.FORCE_CRASH, system_id, f"{system_id}:force_crash")
            # Model a handler that marked CRASHING and fired the NMI but died before finalize:
            # set CRASHING directly (bypassing can_transition), then run the handler as a retry.
            await _set_state(pool, system_id, SystemState.CRASHING.value)
            async with pool.connection() as conn:
                await control_plane.force_crash_handler(conn, job, resolver=resolver)
            assert await _system_state(pool, system_id) == SystemState.CRASHED.value
            assert ctrl.crashed == []  # finalize-only retry: NMI NOT re-fired

    asyncio.run(_run())


def test_force_crash_nmi_raise_propagates_and_leaves_crashing(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id = await _seed_system(pool, SystemState.READY, domain_name="kdive-x")
            ctrl = _RaisingCrashController()
            resolver = provider_resolver(provisioner=_TrackingProvisioner(), controller=ctrl)
            job = await _enqueue(pool, JobKind.FORCE_CRASH, system_id, f"{system_id}:force_crash")
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError):
                    await control_plane.force_crash_handler(conn, job, resolver=resolver)
            # Marker set before the NMI; the raise propagates (worker will requeue) — NOT failed.
            assert await _system_state(pool, system_id) == SystemState.CRASHING.value

    asyncio.run(_run())
```

Add a `_set_state` helper near `_system_state` (a direct UPDATE that bypasses `can_transition`, for test scaffolding of the mid-window state):

```python
async def _set_state(pool: AsyncConnectionPool, system_id: str, state: str) -> None:
    """Direct state UPDATE bypassing can_transition — for scaffolding a mid-window state."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("UPDATE systems SET state = %s WHERE id = %s", (state, system_id))
```

(`dict_row` is already imported at the top of this test file.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run python -m pytest tests/adversarial/test_provider_state_races.py -k "force_crash_drives or retry_after_crashing or nmi_raise" -q`
Expected: FAIL — current handler drives `ready → crashed` directly (no `crashing`), re-fires the NMI on retry, and marks state differently on raise.

- [ ] **Step 3: Rewrite the handler (state-conditional)**

Replace `force_crash_handler`, `_force_crash_target`, and the READY branch of `_finalize_force_crash` in `src/kdive/jobs/handlers/control.py` with:

```python
class _CrashPrecheck(NamedTuple):
    action: str  # "done" | "finalize" | "crash"
    target: _ControlTarget | None


async def _force_crash_precheck(conn: AsyncConnection, system_id: UUID) -> _CrashPrecheck:
    """Classify a force_crash without transitioning (under the SYSTEM lock).

    ``crash`` = first attempt (READY): resolve the controller, then enter CRASHING, then fire.
    ``finalize`` = a retry whose marker is already set: finalize only, no controller, no NMI.
    ``done`` = terminal / already CRASHED: nothing to do.
    """
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        system = await SYSTEMS.get(conn, system_id)
        if system is None:
            raise CategorizedError(
                "force_crash target system is gone",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id)},
            )
        if system.state in TERMINAL_SYSTEM_STATES or system.state is SystemState.CRASHED:
            return _CrashPrecheck("done", None)
        if system.state is SystemState.CRASHING:
            return _CrashPrecheck(
                "finalize", _ControlTarget(_resolved_domain_name(system), system.project)
            )
        if system.state is SystemState.READY:
            return _CrashPrecheck(
                "crash", _ControlTarget(_resolved_domain_name(system), system.project)
            )
        raise CategorizedError(
            "force_crash requires a READY system",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"system_id": str(system_id), "current_status": system.state.value},
            terminal=True,
        )


async def _enter_crashing(conn: AsyncConnection, system_id: UUID) -> _ControlTarget | None:
    """Commit READY -> CRASHING under the lock, the last DB write before the NMI.

    Returns the target, or ``None`` if the state moved out of READY between the precheck and
    here (a raced teardown/finalize) — the caller then skips the NMI.
    """
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        system = await SYSTEMS.get(conn, system_id)
        if system is None or system.state is not SystemState.READY:
            return None
        await SYSTEMS.update_state(conn, system_id, SystemState.CRASHING)
        return _ControlTarget(_resolved_domain_name(system), system.project)


async def force_crash_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
) -> str | None:
    """Crash the guest and drive System ready->crashing->crashed + DebugSession live->detached.

    The CRASHING marker is committed under the SYSTEM lock before the unlocked NMI so the power
    path's non-READY guard refuses the System for the whole NMI-to-CRASHED window (ADR-0325). A
    retry whose marker is already set finalizes without re-firing the NMI; an NMI-call raise
    propagates (the worker requeues) and is resolved evidence-first on retry/by the reconciler.
    """
    system_id = UUID(load_payload(job, SystemPayload).system_id)
    precheck = await _force_crash_precheck(conn, system_id)
    if precheck.action == "done":
        return str(system_id)
    if precheck.action == "finalize":
        assert precheck.target is not None
        await _finalize_force_crash(conn, job, system_id, precheck.target.project)
        return str(system_id)
    # First attempt: resolve the controller while still READY (a failure here leaves READY),
    # then commit CRASHING as the last DB write before the NMI.
    control = await _controller(conn, system_id, resolver)
    target = await _enter_crashing(conn, system_id)
    if target is None:
        return str(system_id)  # raced out of READY; nothing physical to do
    await asyncio.to_thread(control.force_crash, target.domain_name)
    await _finalize_force_crash(conn, job, system_id, target.project)
    return str(system_id)
```

Then change `_finalize_force_crash`'s READY branch to a CRASHING branch:

```python
async def _finalize_force_crash(
    conn: AsyncConnection, job: Job, system_id: UUID, project: str
) -> None:
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        system = await SYSTEMS.get(conn, system_id)
        if system is None:
            raise CategorizedError(
                "force_crash target system is gone",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id)},
            )
        if system.state in TERMINAL_SYSTEM_STATES:
            return
        if system.state is SystemState.CRASHING:
            await SYSTEMS.update_state(conn, system_id, SystemState.CRASHED)
            await audit.record(
                conn,
                job_context_from_job(job, project),
                audit.AuditEvent(
                    tool="control.force_crash",
                    object_kind="systems",
                    object_id=system_id,
                    transition="crashing->crashed",
                    args={"system_id": str(system_id)},
                    project=project,
                ),
            )
        await detach_sessions(conn, job, system)
```

Delete the now-unused `_force_crash_target` function. Ensure `NamedTuple` is imported (it already is at the top).

- [ ] **Step 4: Run to verify they pass**

Run: `uv run python -m pytest tests/adversarial/test_provider_state_races.py -k "force_crash_drives or retry_after_crashing or nmi_raise or concurrent_force_crash_and_teardown" -q`
Expected: PASS (including the existing `concurrent_force_crash_and_teardown` test — the terminal-early-return still holds; update it only if it asserted `ready->crashed` semantics).

- [ ] **Step 5: Update the existing `test_control_tools.py` force_crash-handler tests**

In `tests/mcp/lifecycle/test_control_tools.py`, `test_force_crash_handler_already_crashed_is_idempotent` (~line 626) asserts the **old** re-fire contract. Update it to the new no-op-on-`CRASHED` semantics:

```python
            assert ctrl.crashed == []  # already CRASHED: force_crash is a no-op, NMI not re-fired
```
(was `assert ctrl.crashed == ["kdive-x"]  # NMI re-attempted`)

Also update the stale audit-transition literal in that test's query (~line 640) — the count is still 0 either way (nothing is audited on the no-op), but keep the literal current:

```python
                    "SELECT count(*) AS n FROM audit_log WHERE transition = 'crashing->crashed'"
```

Verify the other two handler tests still pass unchanged: `test_force_crash_handler_crashes_and_detaches` (~line 577 — a fresh `READY` crash still fires the NMI exactly once → `ctrl.crashed == ["kdive-x"]`, state `crashed`, sessions detached) and `test_force_crash_handler_terminal_system_does_not_crash` (~line 648 — `TORN_DOWN` → `ctrl.crashed == []`). No change needed to those two.

Run: `uv run python -m pytest tests/mcp/lifecycle/test_control_tools.py -k force_crash_handler -q`
Expected: PASS.

- [ ] **Step 6: Lint + type**

Run: `just lint && just type`
Expected: clean. (`_finalize_force_crash` and `force_crash_handler` stay under 100 lines / complexity 8.)

- [ ] **Step 7: Commit**

```bash
git add src/kdive/jobs/handlers/control.py tests/adversarial/test_provider_state_races.py tests/mcp/lifecycle/test_control_tools.py
git commit -m "feat(control): force_crash drives ready->crashing->crashed, finalize-only retry (#1078)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Power refuses `crashing` (admission + execution + race) and docstring

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/control.py` (`control.power` wrapper docstring + `action` `Field` ~line 289-316; module docstring ~line 1-15 — no logic change)
- Test: `tests/adversarial/test_provider_state_races.py` (execution + race)
- Test: `tests/mcp/lifecycle/test_control_tools.py` (admission)
- Regenerate: `docs/` tool reference (`just docs`) and RBAC matrix if affected (`just rbac-matrix`)

**Interfaces:**
- Consumes: `power_handler` / `_power_target` (unchanged), `SystemState.CRASHING`, the `_RecordingController` harness.
- Produces: no code-behavior change to `power_handler`; only tests + agent-facing docstring naming `crashing` as refused.

- [ ] **Step 1: Write the failing tests**

Execution + race in `tests/adversarial/test_provider_state_races.py`:

```python
def test_power_refused_on_crashing_no_physical_reset(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id = await _seed_system(pool, SystemState.READY, domain_name="kdive-x")
            await _set_state(pool, system_id, SystemState.CRASHING.value)
            ctrl = _RecordingController()
            resolver = provider_resolver(provisioner=_TrackingProvisioner(), controller=ctrl)
            pjob = await _enqueue(pool, JobKind.POWER, system_id, f"{system_id}:power")
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as excinfo:
                    await control_plane.power_handler(conn, pjob, resolver=resolver)
            assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
            assert excinfo.value.terminal is True
            assert ctrl.powered == []  # the load-bearing property: no physical reset

    asyncio.run(_run())


def test_force_crash_marker_refuses_racing_power(migrated_url: str) -> None:
    # Interleaving A: force_crash commits CRASHING before the power op's re-check -> power refused.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            for i in range(12):
                system_id = await _seed_system(pool, SystemState.READY, domain_name=f"kdive-{i}")
                ctrl = _RecordingController()
                resolver = provider_resolver(provisioner=_TrackingProvisioner(), controller=ctrl)
                cjob = await _enqueue(pool, JobKind.FORCE_CRASH, system_id, f"{system_id}:force_crash")
                pjob = await _enqueue(pool, JobKind.POWER, system_id, f"{system_id}:power")

                async def run_crash(job: Job = cjob, resolver=resolver) -> None:
                    async with pool.connection() as conn:
                        await control_plane.force_crash_handler(conn, job, resolver=resolver)

                async def run_power(job: Job = pjob, resolver=resolver) -> None:
                    async with pool.connection() as conn:
                        try:
                            await control_plane.power_handler(conn, job, resolver=resolver)
                        except CategorizedError:
                            pass  # refused when it saw CRASHING/CRASHED — the safe outcome

                await asyncio.gather(run_crash(), run_power())
                # The System reaches crashed; and the guest is never reset AFTER it crashed:
                # every recorded power op (if any) happened while still READY, before the NMI.
                assert await _system_state(pool, system_id) in {
                    SystemState.CRASHED.value,
                    SystemState.CRASHING.value,
                }
                assert len(ctrl.powered) <= 1  # at most the pre-marker READY power op

    asyncio.run(_run())
```

Admission in `tests/mcp/lifecycle/test_control_tools.py` — mirror the existing `power` admission tests; seed a System, set its state to `crashing`, assert `control.power` returns `configuration_error` with `data.current_status == "crashing"` and no job is enqueued. (Follow the existing power-admission test's fixture/seed pattern in that file.)

- [ ] **Step 2: Run to verify they fail / establish baseline**

Run: `uv run python -m pytest tests/adversarial/test_provider_state_races.py -k "power_refused_on_crashing or marker_refuses_racing_power" -q`
Expected: `power_refused_on_crashing` PASSES already (the existing non-READY guard refuses crashing) — that is the point: it confirms zero power-path change is needed. `marker_refuses_racing_power` PASSES too. If either errors on setup, fix the test, not the handler. The admission test may need the `crashing` `current_status` wording — confirm it passes.

*Note:* these tests assert existing behaviour; they are regression guards for AC1/AC2/AC3. They must be green with **no** `power_handler` change.

- [ ] **Step 3: Update the `control.power` agent-facing contract**

In `src/kdive/mcp/tools/lifecycle/control.py`, the `control.power` wrapper docstring (~line 306) and the `action` `Field` (~line 293) and the `system_id` `Field` (~line 290) currently say "Admitted only on a READY System" / "a CRASHED System holds crash evidence". Extend to name `crashing`:

- Wrapper docstring: "Refused on a non-READY System (a CRASHED **or CRASHING** System holds crash evidence — use the crash workflow)."
- `action` Field: append "Refused on a CRASHED/CRASHING System."
- Update the module docstring's `control.power` line similarly (name the `crashing` transient).

No handler/logic change.

- [ ] **Step 4: Regenerate the committed tool reference**

Run: `just docs && just docs-check`
Expected: `just docs` rewrites the generated reference to include the new wording; `docs-check` then passes. If the RBAC matrix embeds the docstring, run `just rbac-matrix && just rbac-matrix-check`.

- [ ] **Step 5: Run the tests + lint/type**

Run: `uv run python -m pytest tests/adversarial/test_provider_state_races.py tests/mcp/lifecycle/test_control_tools.py -q && just lint && just type`
Expected: PASS / clean.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/mcp/tools/lifecycle/control.py tests/adversarial/test_provider_state_races.py tests/mcp/lifecycle/test_control_tools.py docs/
git commit -m "feat(control): name CRASHING as a power-refused state; race guards (#1078)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Reconciler `repair_stalled_crashing_systems`

**Files:**
- Modify: `src/kdive/reconciler/repairs/systems.py` (add the repair)
- Modify: `src/kdive/reconciler/loop.py` (module alias ~line 103; `_REPAIR_CATALOG` ~line 375 — insert **after** `"abandoned_jobs"`)
- Test: `tests/reconciler/test_stalled_crashing_recovery.py` (create)

**Interfaces:**
- Consumes: `SystemState.CRASHING/CRASHED`, `DebugSessionState`, `advisory_xact_lock`, `SYSTEMS.get`/`SYSTEMS.update_state`, `audit.record_system`, `JobState`, dedup_key `{system_id}:force_crash`. (Task 5 does **not** modify `control.py` — the session detach is a local `_detach_sessions_reconciler`.)
- Produces: `async def repair_stalled_crashing_systems(conn) -> int`; catalog kind `"stalled_crashing_systems"` (auto-joins `ALL_REPAIR_KINDS` via `_REPAIR_CATALOG`).

- [ ] **Step 1: Write the failing tests**

Create `tests/reconciler/test_stalled_crashing_recovery.py`. Model each case: a `crashing` System plus a `force_crash` job in a given state, then run the repair and assert the outcome. Use the same seeding helpers the sibling reconciler tests use (`tests/reconciler/conftest` fixtures / `migrated_url`). Cases:

```python
# AC5: no active force_crash job (FAILED / CANCELED / absent) -> crashed + detach + audit.
def test_recovers_crashing_with_failed_job(migrated_url): ...       # job FAILED -> system crashed
def test_recovers_crashing_with_canceled_job(migrated_url): ...     # job CANCELED -> system crashed
def test_recovers_crashing_with_no_job_row(migrated_url): ...       # no force_crash row -> crashed
# AC5, detach path (guards the hand-copied _detach_sessions_reconciler SQL):
def test_recovers_crashing_detaches_live_session(migrated_url): ...  # LIVE session -> detached + audited
# AC5a: active job -> left alone.
def test_leaves_crashing_with_running_valid_lease(migrated_url): ...  # running, lease ahead -> crashing
def test_leaves_crashing_with_queued_job(migrated_url): ...          # queued -> crashing
# AC5b: lease-lapsed running with attempts remaining -> left alone this tick (a worker reclaims).
def test_leaves_crashing_with_lease_lapsed_running(migrated_url): ...  # running, lease past, attempt<max -> crashing
```

The `test_recovers_crashing_detaches_live_session` case is the guard for the new hand-copied
`_detach_sessions_reconciler` SQL: seed a `crashing` System with a `FAILED` force_crash job **and**
a Run carrying a `LIVE` DebugSession, run the repair, then assert (a) the System is `crashed`,
(b) the DebugSession is `detached`, and (c) an `audit_log` row exists with
`object_kind='debug_sessions'`, `transition='live->detached'`, and `principal='system:reconciler'`.
Reuse the session-seeding pattern from `tests/mcp/lifecycle/test_control_tools.py`'s
`_seed_live_session` / the force_crash detach test.

Each recover-case asserts `state == "crashed"` after the repair and that the repair returned `1`; each leave-alone case asserts `state == "crashing"` and the repair returned `0`. For the recover cases, also assert an audit row with `transition = "crashing->crashed"`. (Follow the audit-row assertion style in `tests/security/test_audit.py`.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run python -m pytest tests/reconciler/test_stalled_crashing_recovery.py -q`
Expected: FAIL — `repair_stalled_crashing_systems` does not exist.

- [ ] **Step 3: Implement the repair**

Add to `src/kdive/reconciler/repairs/systems.py`:

**Imports — a diff against the *existing* `systems.py` header (do not re-import what is
already there).** `systems.py` already imports `LockScope, advisory_xact_lock`,
`from kdive.domain.capacity.state import AllocationState, SystemState`, and
`SYSTEM_RECONCILER_PRINCIPAL`. Make exactly these changes:
- **Extend** the state import: `from kdive.domain.capacity.state import AllocationState, DebugSessionState, JobState, SystemState`
- **Add** `from kdive.db.repositories import SYSTEMS`
- **Add** `from kdive.security import audit`
Do **not** re-add `LockScope`/`advisory_xact_lock`/`SystemState`/`SYSTEM_RECONCILER_PRINCIPAL`
(already present — a duplicate is a ruff F811).

```python
_ACTIVE_JOB_STATE_VALUES = (JobState.QUEUED.value, JobState.RUNNING.value)


async def repair_stalled_crashing_systems(conn: AsyncConnection) -> int:
    """Recover a `crashing` System whose force_crash job can never run again -> `crashed`.

    A `crashing` System's force_crash NMI has (overwhelmingly) already fired; if no force_crash
    job is still active (queued/running) — dead-lettered `failed`, operator-`canceled`, or the
    invariant-only absent row — the handler stopped before finalize, so the System would strand
    forever with power blocked. Resolve it evidence-first to `crashed` (ADR-0325). A still-active
    job (running with a valid or lapsed lease, or queued) is left for the worker/retry path.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT s.id, s.project FROM systems s "
            "WHERE s.state = %s "
            "  AND NOT EXISTS ( "
            "    SELECT 1 FROM jobs j "
            "    WHERE j.dedup_key = s.id::text || ':force_crash' "
            "      AND j.state = ANY(%s) "
            "  )",
            (SystemState.CRASHING.value, list(_ACTIVE_JOB_STATE_VALUES)),
        )
        candidates = await cur.fetchall()
    recovered = 0
    for candidate in candidates:
        system_id: UUID = candidate["id"]
        async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
            system = await SYSTEMS.get(conn, system_id)
            if system is None or system.state is not SystemState.CRASHING:
                continue
            await SYSTEMS.update_state(conn, system_id, SystemState.CRASHED)
            await audit.record_system(
                conn,
                principal=SYSTEM_RECONCILER_PRINCIPAL,
                event=audit.AuditEvent(
                    tool="control.force_crash",
                    object_kind="systems",
                    object_id=system_id,
                    transition="crashing->crashed",
                    args={"system_id": str(system_id)},
                    project=system.project,
                ),
            )
            await _detach_sessions_reconciler(conn, system)
        recovered += 1
        _log.info("reconciler: stalled crashing system %s -> crashed", system_id)
    return recovered
```

`detach_sessions` in `control.py` takes a `Job` and audits each detach via `audit.record` (needs a `RequestContext`); the reconciler has neither. Add `_detach_sessions_reconciler(conn, system)` in `systems.py` that mirrors `detach_sessions`' SQL (the `WITH targets ... UPDATE debug_sessions ... RETURNING` statement) but audits each detached session with `audit.record_system(conn, principal=SYSTEM_RECONCILER_PRINCIPAL, event=AuditEvent(tool="control.force_crash", object_kind="debug_sessions", object_id=session_id, transition=f"{old_state}->detached", args={"system_id": str(system.id)}, project=system.project))`. Keep it small and local to the repair; do not refactor `detach_sessions`' `Job`-based signature (the force_crash call site still uses it as-is).

The Step 3 predicate `j.dedup_key = s.id::text || ':force_crash'` matches the enqueue key
`f"{system_id}:force_crash"` (`mcp/tools/lifecycle/control.py:207`); `jobs.dedup_key` is `text`
(schema `0001_init.sql:166`), so the concat/compare typechecks.

- [ ] **Step 4: Verify the dedup_key predicate against the enqueue site**

Read `src/kdive/mcp/tools/lifecycle/control.py:207` and confirm the force_crash enqueue key is exactly `f"{system_id}:force_crash"`. If it differs, adjust the Step 3 predicate to match. (This is a one-line verification; the Step 3 code already ships the correct form.)

- [ ] **Step 5: Register the repair in the reconciler loop**

In `src/kdive/reconciler/loop.py`: add near the other module aliases (~line 103):

```python
_repair_stalled_crashing_systems = system_repairs.repair_stalled_crashing_systems
```

add `"_repair_stalled_crashing_systems"` to the `__all__`-style export tuple (~line 121), and insert a catalog entry **immediately after** the `"abandoned_jobs"` entry (so it runs after `repair_abandoned_jobs` dead-letters zombies) in `_REPAIR_CATALOG` (~line 375):

```python
    _RepairCatalogEntry("abandoned_jobs", lambda _r, _c, _g: _repair_abandoned_jobs),
    _RepairCatalogEntry(
        "stalled_crashing_systems", lambda _r, _c, _g: _repair_stalled_crashing_systems
    ),
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run python -m pytest tests/reconciler/test_stalled_crashing_recovery.py tests/reconciler/test_loop.py -q`
Expected: PASS. `test_loop.py` includes `test_all_repair_kinds_matches_a_fully_populated_plan` — it validates the new kind is wired into `ALL_REPAIR_KINDS`; if it fails, the catalog entry or the `ALL_REPAIR_KINDS` source needs the new kind.

- [ ] **Step 7: Lint + type**

Run: `just lint && just type`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add src/kdive/reconciler/repairs/systems.py src/kdive/reconciler/loop.py tests/reconciler/test_stalled_crashing_recovery.py
git commit -m "feat(reconciler): recover stalled crashing Systems to crashed (#1078)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Full guardrail sweep + no-dead-edge verification (AC7)

**Files:** none (verification + any fixups surfaced).

- [ ] **Step 1: Confirm `READY → CRASHED` has no remaining producer**

Run: `rg -n "SystemState.CRASHED" src/kdive` and confirm the only writer of `CRASHED` is `_finalize_force_crash` (CRASHING→CRASHED) and the reconciler repair (CRASHING→CRASHED). No path transitions `READY → CRASHED`. If any exists, either route it through `CRASHING` or restore the edge with justification (do not silently leave a dead edge).

- [ ] **Step 2: Sweep the `ready->crashed` audit literal across `tests/` (including gated suites)**

`force_crash` now audits the system transition as `crashing->crashed`. `just ci` runs `-m "not live_vm and not live_stack"`, so it does **not** catch the gated live-stack proof. Grep and fix:

Run: `rg -n "ready->crashed" tests/ src/`
Expected hits to fix:
- `tests/integration/live_stack/spine.py:367` — change `transition="ready->crashed"` to `transition="crashing->crashed"` (this is a `live_stack` proof `just ci` does **not** run; this dev host runs live proofs, so update it or the on-host proof fails). The `crashing->crashed` literal itself is guarded under `just ci` by the system-audit assertion added in Task 3 Step 1 (`test_force_crash_drives_ready_crashing_crashed`), so a stray rename of the literal is caught by CI even though this specific `spine.py` assertion is not.
- `tests/mcp/lifecycle/test_control_tools.py:640` — already handled in Task 4 Step 5 (the count query literal).
- Comment-only hits (e.g. `test_control_tools.py:422`, `test_provider_state_races.py:251`) — update the prose to `ready->crashing->crashed` for accuracy; no assertion depends on them.

- [ ] **Step 3: Sweep stale `ready -> crashed` prose in `src/` docstrings/comments**

The two-step path should read consistently everywhere it is described. Grep and update each to name the `crashing` transient:

Run: `rg -n "ready ?-> ?crashed|ready->crashed" src/kdive`
Update: `src/kdive/mcp/tools/lifecycle/control.py` module docstring (~lines 8, 14, 84) and `src/kdive/jobs/handlers/control.py` (the `force_crash_handler` docstring — already rewritten in Task 3), plus any comment in `console_rotate.py` / `state.py` that describes the force_crash lifecycle. Then re-run `just docs && just docs-check` so the generated reference matches (the `control.py` module docstring may not serialize into the tool schema, but keep it accurate).

- [ ] **Step 4: Run the full gate**

Run: `just ci`
Expected: green (lint, `type`, lint-shell, lint-workflows, check-mermaid, test). Also run `just docs-check`, `just adr-status-check`, `just rbac-matrix-check` if not already covered by `ci`.

- [ ] **Step 5: Run the gated proofs this host can run**

This dev host runs KVM/libvirt live proofs. Run the live-stack spine proof (or at least confirm the edited `spine.py` assertion compiles/imports) so the `crashing->crashed` audit change is not silently broken by `just ci`'s exclusion:

Run: `just test-live-stack` (skips cleanly if the stack/fixtures are absent — note the skip if so, do not treat absence as pass).

- [ ] **Step 6: Fix any drift and re-run**

Address every failure (generated-doc drift → re-run `just docs`/`just rbac-matrix`; type/lint → fix). Re-run `just ci` until green.

- [ ] **Step 7: Commit any fixups**

```bash
git add <explicit fixed paths>
git commit -m "chore(control): guardrail fixups for the crashing-state change (#1078)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review (author checklist — completed)

- **Spec coverage:** R1 → Task 4 (AC3/AC3b) + the marker in Task 3; R2 → Task 4 (AC2); R3 → Task 5 (AC5/AC5a/AC5b); R4 → Task 3 (AC4/AC4a/AC4b); R5 → no power logic change (Task 4). Fan-out table → Task 2 (AC6). Migration → Task 1. AC7 → Task 6.
- **Existing-test breakage (plan-review findings, now covered):** `tests/domain/test_state.py:214-219` (Task 1 — flip the removed `READY→CRASHED` edge); `tests/mcp/lifecycle/test_control_tools.py` idempotent test (Task 4 Step 5 — no NMI re-fire on `CRASHED`); `tests/integration/live_stack/spine.py:367` + the `ready->crashed` audit literal (Task 6 Steps 2-3, incl. the `live_stack` proof `just ci` excludes).
- **Placeholder scan:** all shown test blocks are copy-clean (the illegal `CRASHED→READY` scaffold was removed — the retry test sets `CRASHING` directly via `_set_state`). The reconciler helper and the `dedup_key` SQL are inlined correctly (predicate `s.id::text || ':force_crash'`; import block a diff against the real `systems.py` header). The new `_detach_sessions_reconciler` SQL is guarded by `test_recovers_crashing_detaches_live_session` (Task 5).
- **CI visibility of the audit literal:** `crashing->crashed` is asserted by a non-gated test (`test_force_crash_drives_ready_crashing_crashed`), so `just ci` catches a rename even though the `live_stack` proof that also uses it is excluded from CI.
- **Type consistency:** `_ControlTarget`, `_CrashPrecheck`, `_resolved_domain_name`, `_controller`, `detach_sessions`, `SYSTEMS.update_state(conn, id, state)`, `advisory_xact_lock(conn, LockScope.SYSTEM, id)` all match the current `control.py`. `SystemState.CRASHING` is defined in Task 1 before every consumer.
