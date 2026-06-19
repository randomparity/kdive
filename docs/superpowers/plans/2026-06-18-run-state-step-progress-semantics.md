# Run.state vs run_steps progress semantics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `runs.get` expose install/boot progress (a `data.steps` map + a progression-aware `suggested_next_actions`) so a caller can tell that `Run.state.succeeded` means *build* succeeded, and pick the next lifecycle tool without guessing.

**Architecture:** Additive read-side change. A new `services/runs/steps.py` helper reads the `install`/`boot` rows of the `run_steps` ledger (state + the `boot` row's `boot_outcome`). `runs.get` (`view.py`) passes that into `envelope_for_run` (`common.py`), which on a `SUCCEEDED` Run adds `data.steps` and walks `suggested_next_actions`. No state machine, schema, transport, or migration change.

**Tech Stack:** Python 3.14, `uv`, `psycopg` (async, `dict_row`), `pytest`, `ty`, `ruff`. Postgres via testcontainers (tests need a Docker daemon; they skip without one).

## Global Constraints

- ADR: [ADR-0179](../../adr/0179-run-state-step-progress-semantics.md); spec: [docs/specs/2026-06-18-run-state-step-progress-semantics.md](../../specs/2026-06-18-run-state-step-progress-semantics.md).
- No rename or reshape of `RunState`; no migration; no schema change.
- `data.steps` is surfaced **only** on a `SUCCEEDED` Run. Keys are exactly `build`, `install`, `boot`. Values are `succeeded` / `running` / `pending` (`pending` = no row). `build` is always `succeeded` on a `SUCCEEDED` Run (by construction).
- Booted-run next-action keys on the **observed** `boot_outcome` from the `boot` step result, never on `run.expected_boot_failure`. `expected_crash_observed` → `["postmortem.triage", "vmcore.fetch"]`; any other/absent outcome → `["debug.start_session"]`.
- Persisted step vocabulary stays `{running, succeeded}` (ADR-0171). `pending` is read-surface synthesis only. A `running` claim is read as persisted (no liveness reinterpretation).
- Absolute imports only. Ruff line length 100. Lint set `E,F,I,UP,B,SIM`. `ty` strict, whole-tree.
- Guardrail commands (run before each commit): `just lint`, `just type`, and the focused test for the file you touched (`uv run python -m pytest <path>::<test> -q`). Before the first push, run the full `just ci` and `just docs-check`.
- Commit trailer required: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

## File Structure

- `src/kdive/services/runs/steps.py` — **modify**: add `StepProgress` dataclass + `async def step_progress(conn, run_id)`.
- `src/kdive/mcp/tools/lifecycle/runs/common.py` — **modify**: extend `envelope_for_run` with a `step_progress` param; add `data.steps` + progression next-actions on `SUCCEEDED`.
- `src/kdive/mcp/tools/lifecycle/runs/view.py` — **modify**: call `step_progress` and pass it to `envelope_for_run`.
- `src/kdive/domain/capacity/state.py` — **modify**: `RunState` docstring (semantics note).
- `src/kdive/mcp/tools/lifecycle/runs/registrar.py` — **modify**: `runs.get` docstring (semantics note for the generated reference).
- `docs/guide/reference/runs.md` — **regenerate** via `just docs`.
- `tests/mcp/lifecycle/test_runs_tools.py` — **modify**: unit test for `step_progress` + `runs.get` envelope cases.

---

### Task 1: `step_progress` ledger helper

**Files:**
- Modify: `src/kdive/services/runs/steps.py` (add after `existing_build_result`, ~line 85)
- Test: `tests/mcp/lifecycle/test_runs_tools.py`

**Interfaces:**
- Consumes: an `AsyncConnection` and the run's `UUID`.
- Produces:
  - `StepProgress` — `@dataclass(frozen=True, slots=True)` with `install: str`, `boot: str`, `boot_outcome: str | None`, and `def steps_map(self) -> dict[str, str]` returning `{"build": "succeeded", "install": self.install, "boot": self.boot}`.
  - `async def step_progress(conn: AsyncConnection, run_id: UUID) -> StepProgress` — reads the `install`/`boot` rows; missing row → `"pending"`; `boot_outcome` from the `boot` row's `result` JSON (`None` when absent or boot not recorded).

- [ ] **Step 1: Write the failing test**

Add near the other `runs.get` tests in `tests/mcp/lifecycle/test_runs_tools.py` (import `step_progress` and `StepProgress` from `kdive.services.runs.steps` at the top with the existing `run_steps` import group):

```python
def test_step_progress_reads_install_boot_and_outcome(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO run_steps (run_id, step, state, result) "
                    "VALUES (%s, 'install', 'succeeded', %s)",
                    (UUID(run_id), Jsonb({})),
                )
                await conn.execute(
                    "INSERT INTO run_steps (run_id, step, state, result) "
                    "VALUES (%s, 'boot', 'succeeded', %s)",
                    (UUID(run_id), Jsonb({"boot_outcome": "expected_crash_observed"})),
                )
                progress = await step_progress(conn, UUID(run_id))
        assert progress == StepProgress(
            install="succeeded", boot="succeeded", boot_outcome="expected_crash_observed"
        )
        assert progress.steps_map() == {
            "build": "succeeded",
            "install": "succeeded",
            "boot": "succeeded",
        }

    asyncio.run(_run())


def test_step_progress_missing_rows_are_pending(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            async with pool.connection() as conn:
                progress = await step_progress(conn, UUID(run_id))
        assert progress == StepProgress(install="pending", boot="pending", boot_outcome=None)

    asyncio.run(_run())
```

If `Jsonb` is not already imported in the test module, add `from psycopg.types.json import Jsonb`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest "tests/mcp/lifecycle/test_runs_tools.py::test_step_progress_reads_install_boot_and_outcome" "tests/mcp/lifecycle/test_runs_tools.py::test_step_progress_missing_rows_are_pending" -q`
Expected: FAIL with `ImportError` / `cannot import name 'step_progress'`.

- [ ] **Step 3: Write minimal implementation**

In `src/kdive/services/runs/steps.py`, add (the module already imports `AsyncConnection`, `dict_row`, `UUID`, `cast`, `Mapping`, `dataclass`):

```python
_PROGRESS_STEPS = ("install", "boot")


@dataclass(frozen=True, slots=True)
class StepProgress:
    """Install/boot progress for a built Run, read from the `run_steps` ledger (ADR-0179)."""

    install: str
    boot: str
    boot_outcome: str | None

    def steps_map(self) -> dict[str, str]:
        """The fixed-key `runs.get` `data.steps` map; `build` is `succeeded` by construction."""
        return {"build": "succeeded", "install": self.install, "boot": self.boot}


async def step_progress(conn: AsyncConnection, run_id: UUID) -> StepProgress:
    """Read the `install`/`boot` ledger rows for a built Run (ADR-0179).

    A missing row is reported as ``pending`` (the step has not started); a present row
    carries its persisted ``running``/``succeeded`` state verbatim. ``boot_outcome`` is the
    ``boot`` step result's recorded outcome (``None`` when boot is unrecorded or carries no
    outcome), used to route the booted-run next-action.
    """
    states = {step: "pending" for step in _PROGRESS_STEPS}
    boot_outcome: str | None = None
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT step, state, result FROM run_steps WHERE run_id = %s AND step = ANY(%s)",
            (run_id, list(_PROGRESS_STEPS)),
        )
        rows = await cur.fetchall()
    for row in rows:
        states[row["step"]] = row["state"]
        if row["step"] == "boot":
            result = row["result"]
            if isinstance(result, Mapping):
                outcome = cast("Mapping[str, object]", result).get("boot_outcome")
                boot_outcome = outcome if isinstance(outcome, str) else None
    return StepProgress(
        install=states["install"], boot=states["boot"], boot_outcome=boot_outcome
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest "tests/mcp/lifecycle/test_runs_tools.py::test_step_progress_reads_install_boot_and_outcome" "tests/mcp/lifecycle/test_runs_tools.py::test_step_progress_missing_rows_are_pending" -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Lint + type, then commit**

Run: `just lint && just type`
Expected: clean.

```bash
git add src/kdive/services/runs/steps.py tests/mcp/lifecycle/test_runs_tools.py
git commit -m "feat(runs): add step_progress ledger helper for install/boot status

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Surface `data.steps` + progression next-actions on `runs.get`

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/runs/common.py:59-106` (`envelope_for_run`)
- Modify: `src/kdive/mcp/tools/lifecycle/runs/view.py:47-58`
- Test: `tests/mcp/lifecycle/test_runs_tools.py`

**Interfaces:**
- Consumes: `StepProgress` and `step_progress` from Task 1.
- Produces: `envelope_for_run(..., step_progress: StepProgress | None = None)`; on a `SUCCEEDED` Run with `step_progress` provided, `data["steps"]` is `step_progress.steps_map()` and `suggested_next_actions` follows the progression table.

- [ ] **Step 1: Write the failing tests**

Add to `tests/mcp/lifecycle/test_runs_tools.py`. Helper to insert a step (put it near the other module-level test helpers):

```python
async def _insert_step(
    pool: AsyncConnectionPool, run_id: str, step: str, state: str, result: dict[str, Any]
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) VALUES (%s, %s, %s, %s)",
            (UUID(run_id), step, state, Jsonb(result)),
        )
```

```python
def test_get_built_only_run_steps_and_install_action(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.data["steps"] == {
            "build": "succeeded", "install": "pending", "boot": "pending"
        }
        assert resp.suggested_next_actions == ["runs.get", "runs.install"]

    asyncio.run(_run())


def test_get_install_running_run_recommends_install(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _insert_step(pool, run_id, "install", "running", {})
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.data["steps"]["install"] == "running"
        assert resp.suggested_next_actions == ["runs.get", "runs.install"]

    asyncio.run(_run())


def test_get_installed_run_recommends_boot(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _insert_step(pool, run_id, "install", "succeeded", {})
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.data["steps"] == {
            "build": "succeeded", "install": "succeeded", "boot": "pending"
        }
        assert resp.suggested_next_actions == ["runs.get", "runs.boot"]

    asyncio.run(_run())


def test_get_booted_run_recommends_debug_start_session(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _insert_step(pool, run_id, "install", "succeeded", {})
            await _insert_step(pool, run_id, "boot", "succeeded", {"boot_outcome": "ready"})
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.data["steps"]["boot"] == "succeeded"
        assert resp.suggested_next_actions == ["runs.get", "debug.start_session"]

    asyncio.run(_run())


def test_get_expected_crash_boot_recommends_triage(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _insert_step(pool, run_id, "install", "succeeded", {})
            await _insert_step(
                pool, run_id, "boot", "succeeded", {"boot_outcome": "expected_crash_observed"}
            )
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.suggested_next_actions == [
            "runs.get", "postmortem.triage", "vmcore.fetch"
        ]

    asyncio.run(_run())


def test_get_non_succeeded_run_has_no_steps(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.CREATED)
            resp = await get_run(pool, _ctx(), run_id)
        assert "steps" not in resp.data

    asyncio.run(_run())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py -q -k "steps or recommends or booted or no_steps or triage"`
Expected: FAIL — `KeyError: 'steps'` / next-action assertions mismatch (current code always returns `runs.install`).

- [ ] **Step 3: Implement `envelope_for_run` change**

In `src/kdive/mcp/tools/lifecycle/runs/common.py`, import the helper type at the top with the other `kdive.services.runs` imports:

```python
from kdive.services.runs.steps import StepProgress
```

Replace the `SUCCEEDED` next-action block and the `data` assembly (lines 83-96) so the signature gains `step_progress` and the `SUCCEEDED` branch uses it:

```python
def envelope_for_run(
    run: Run,
    *,
    required_cmdline: str | None = None,
    failing_job: Job | None = None,
    active_debug_session_ids: list[str] | None = None,
    step_progress: StepProgress | None = None,
) -> ToolResponse:
```

(keep the existing docstring; append a sentence: ``step_progress`` (ADR-0179), when supplied on a ``SUCCEEDED`` Run, adds the ``build``/``install``/``boot`` ``data.steps`` map and drives the install→boot→debug progression in ``suggested_next_actions``.)

Then in the body:

```python
    if run.state is RunState.FAILED:
        category = run.failure_category or ErrorCategory.INFRASTRUCTURE_FAILURE
        return _failed_envelope(run, category, failing_job)
    steps: dict[str, str] | None = None
    if run.state in (RunState.CREATED, RunState.RUNNING):
        actions = ["runs.get", "runs.build"]
    elif run.state is RunState.SUCCEEDED:
        actions = ["runs.get", *_succeeded_next_step(run, step_progress)]
        if step_progress is not None:
            steps = step_progress.steps_map()
    else:  # CANCELED — terminal, nothing to advance.
        actions = ["runs.get"]
    data: dict[str, JsonValue] = {
        "project": run.project,
        "target_kind": run.target_kind.value,
        "system_id": str(run.system_id) if run.system_id is not None else None,
        "active_debug_session_ids": list(active_debug_session_ids or []),
    }
    if steps is not None:
        data["steps"] = cast(JsonValue, steps)
```

Add the progression helper above `envelope_for_run`:

```python
def _succeeded_next_step(run: Run, progress: StepProgress | None) -> list[str]:
    """Second action(s) for a SUCCEEDED Run, walking the real progression (ADR-0179)."""
    if run.system_id is None:
        return ["runs.bind"]
    if progress is None or progress.install != "succeeded":
        return ["runs.install"]
    if progress.boot != "succeeded":
        return ["runs.boot"]
    if progress.boot_outcome == "expected_crash_observed":
        return ["postmortem.triage", "vmcore.fetch"]
    return ["debug.start_session"]
```

`cast` and `JsonValue` are already imported in this module.

- [ ] **Step 4: Wire `runs.get` to fetch progress**

In `src/kdive/mcp/tools/lifecycle/runs/view.py`, import the helper:

```python
from kdive.services.runs.steps import step_progress as _step_progress
```

Inside `get_run`, after `active_sessions = await active_session_ids_for_run(conn, run.id)` (still inside the `async with pool.connection() as conn` block), add:

```python
            progress = (
                await _step_progress(conn, run.id)
                if run.state is RunState.SUCCEEDED
                else None
            )
```

and pass it through:

```python
        return envelope_for_run(
            run,
            required_cmdline=required,
            failing_job=failing_job,
            active_debug_session_ids=active_sessions,
            step_progress=progress,
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py -q -k "steps or recommends or booted or no_steps or triage or get_bound or get_unbound"`
Expected: PASS (existing `test_get_bound_succeeded_run_points_to_install` and `test_get_unbound_succeeded_run_points_to_bind` still pass).

- [ ] **Step 6: Lint + type, then commit**

Run: `just lint && just type`
Expected: clean.

```bash
git add src/kdive/mcp/tools/lifecycle/runs/common.py src/kdive/mcp/tools/lifecycle/runs/view.py tests/mcp/lifecycle/test_runs_tools.py
git commit -m "feat(runs): surface install/boot steps + progression on runs.get

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Document the semantics (docstring, tool description, generated reference)

**Files:**
- Modify: `src/kdive/domain/capacity/state.py:86-93` (`RunState` docstring)
- Modify: `src/kdive/mcp/tools/lifecycle/runs/registrar.py:85` (`runs.get` docstring)
- Regenerate: `docs/guide/reference/runs.md`

**Interfaces:** none (docs only).

- [ ] **Step 1: Update the `RunState` docstring**

In `src/kdive/domain/capacity/state.py`, replace the `RunState` docstring:

```python
class RunState(StrEnum):
    """Build-phase lifecycle of a Run; one build per Run, a failed step is terminal.

    ``succeeded`` means the **build** step succeeded — not that the kernel is installed or
    booted. Install and boot progress live in the ``run_steps`` ledger and are surfaced by
    ``runs.get`` as ``data.steps`` (ADR-0179). A failed install/boot step fails the Run to
    ``failed``.
    """
```

- [ ] **Step 2: Update the `runs.get` docstring (drives the generated reference)**

In `src/kdive/mcp/tools/lifecycle/runs/registrar.py`, change the `runs_get` docstring (single line, no `|`):

```python
        """Return one run; `succeeded` means build-succeeded — see `data.steps` for install/boot progress."""
```

- [ ] **Step 3: Regenerate the tool reference**

Run: `just docs`
Then confirm the `runs.get` entry changed: `git diff docs/guide/reference/runs.md`
Expected: the `runs.get` description line now mentions build-succeeded / `data.steps`.

- [ ] **Step 4: Verify the generated reference is in sync**

Run: `just docs-check`
Expected: no staleness error (exit 0).

- [ ] **Step 5: Lint + type, then commit**

Run: `just lint && just type`
Expected: clean.

```bash
git add src/kdive/domain/capacity/state.py src/kdive/mcp/tools/lifecycle/runs/registrar.py docs/guide/reference/runs.md
git commit -m "docs(runs): clarify succeeded = build-succeeded; point at data.steps

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Full guardrails

**Files:** none (verification only).

- [ ] **Step 1: Run the full CI gate**

Run: `just ci`
Expected: lint, type, lint-shell, lint-workflows, check-mermaid, test all pass. (db/integration tests need a Docker daemon — if absent locally, note it; CI runs them.)

- [ ] **Step 2: Run the ADR + docs guards**

Run: `just adr-status-check && just docs-paths && just docs-check`
Expected: all pass.

- [ ] **Step 3: If anything failed, fix and re-run the focused test + guardrail, then amend the owning commit (pre-push only).**

---

## Self-Review

**Spec coverage:**
- "`data.steps` on a `SUCCEEDED` Run, fixed keys, succeeded/running/pending" → Task 1 (helper) + Task 2 (surfacing) + tests.
- "non-`SUCCEEDED` Run carries no `data.steps`" → Task 2 `test_get_non_succeeded_run_has_no_steps`.
- "next-actions walk unbound→bind, built→install, installed→boot, booted-normally→debug.start_session, crashed-as-expected→triage" → Task 2 `_succeeded_next_step` + 6 tests.
- "booted-run branch keys on observed `boot_outcome`" → Task 1 reads `boot_outcome`; Task 2 `_succeeded_next_step`; `test_get_booted_run_*` (ready) vs `test_get_expected_crash_boot_*`.
- "`RunState` docstring + `runs.get` docs state succeeded = build-succeeded" → Task 3.
- "no schema/migration/state-machine change" → no migration file, no `domain/state.py` edit.

**Placeholder scan:** none — every code/test step shows the code.

**Type consistency:** `StepProgress(install, boot, boot_outcome)` and `.steps_map()` are defined in Task 1 and used unchanged in Task 2; `step_progress(conn, run_id)` signature matches view.py call; `_succeeded_next_step(run, progress)` returns `list[str]` spread into `["runs.get", *...]`.
