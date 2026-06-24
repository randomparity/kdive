# Plan — `runs.get` failed-boot evidence (#750)

- **Spec:** [2026-06-23-runs-get-failed-boot-evidence.md](../specs/2026-06-23-runs-get-failed-boot-evidence.md)
- **ADR:** [ADR-0230](../../adr/0230-runs-get-failed-boot-evidence.md)
- **Date:** 2026-06-23

## Where this fits

Closes #750: `runs.get` cannot distinguish a terminally-failed boot from a never-attempted one
(both read `data.steps.boot:"pending"` because the failed boot ledger row is deleted by design,
ADR-0185). The fix is an additive read: fetch the surviving boot job by its deterministic
`dedup_key` and, when it is terminally `failed`, surface `data.boot_readiness =
{job_id, status:"failed", error_category}`.

The tasks are **tightly coupled** (one helper, one view-path call, one envelope render, all in
the `runs.get` read path), so this is implemented **directly in this session** with TDD, not
fanned out to independent subagents.

## Repo conventions that apply

- Guardrails before every commit: `just lint` (`ruff check` + `ruff format --check`), `just type`
  (whole-tree `ty`), focused tests via `uv run python -m pytest <path> -q`. Full `just test`
  once before first push.
- Tests mirror the package tree under `tests/`. Run-tool tests live in
  `tests/mcp/lifecycle/test_runs_tools.py`; service-layer step tests near
  `tests/services/runs/` (locate the existing `step_progress` test).
- `ErrorCategory` is the stable taxonomy (`domain/errors.py`); never invent strings.
- `ToolResponse` envelope: `data` is a free-form `dict[str, JsonValue]`; no per-field schema.
- Absolute imports only; ≤100-char lines; Google-style docstrings on non-trivial public APIs.
- Conventional commits, `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

## Tasks

### Task 1 — `BootAttempt` + `failed_boot_attempt` read helper (`services/runs/steps.py`)

**Failing test first.** `failed_boot_attempt` needs a DB connection, so put these in a
DB-backed module. `tests/services/runs/test_steps.py` is pure-unit (no DB); the runs DB
fixtures (`_pool`, `_seed_*`, `enqueue`) live in `tests/mcp/lifecycle/test_runs_tools.py`. Add
the helper's DB tests there alongside the `get_run` tests (Task 3), or factor a small DB-backed
`tests/services/runs/test_failed_boot_attempt.py` reusing the same `testcontainers` Postgres
fixture the runs-tool suite uses. Cases:

- `test_failed_boot_attempt_returns_none_when_no_boot_job` — no boot job for the run → `None`.
- `test_failed_boot_attempt_returns_none_for_queued_job` — enqueue a boot job (state `queued`) →
  `None`.
- `test_failed_boot_attempt_returns_none_for_running_job` — boot job `running` → `None`.
- `test_failed_boot_attempt_surfaces_failed_job` — boot job `failed` with
  `error_category=READINESS_FAILURE` → `BootAttempt(job_id=<job.id>, error_category=READINESS_FAILURE)`.
- `test_failed_boot_attempt_null_category` — boot job `failed` with `error_category=None` →
  `BootAttempt(job_id=<job.id>, error_category=None)`.

**Test setup — driving a boot job to `failed`:** `queue.enqueue` inserts a job as `queued`, so
the failed-state tests must transition it explicitly. Follow the existing repo pattern: enqueue
with `dedup_key=f"{run_id}:boot"`, then run a direct
`UPDATE jobs SET state='failed', error_category=%s WHERE dedup_key=%s` on the same `conn` (cf.
`_enqueue_with_state` and the inline `UPDATE jobs SET …` seeds in `tests/jobs/test_queue.py`).
For the null-category case set `error_category` to `NULL`. Do **not** drive a real worker; seed
the terminal state directly so the test isolates `failed_boot_attempt`'s read logic.

**Implementation:**

```python
@dataclass(frozen=True, slots=True)
class BootAttempt:
    """The terminally-failed boot job behind a deleted boot step (#750, ADR-0230)."""
    job_id: UUID
    error_category: ErrorCategory | None

    def as_data(self) -> dict[str, JsonValue]:
        return {
            "job_id": str(self.job_id),
            "status": "failed",
            "error_category": self.error_category.value if self.error_category else None,
        }
```

```python
async def failed_boot_attempt(conn: AsyncConnection, run_id: UUID) -> BootAttempt | None:
    job = await queue.get_by_dedup_key(conn, f"{run_id}:boot")
    if job is None or job.state is not JobState.FAILED:
        return None
    return BootAttempt(job_id=job.id, error_category=job.error_category)
```

- Import `JobState` (`domain/capacity/state`), `queue` (`kdive.jobs`), `JsonValue`
  (`mcp/responses`). Confirm no import cycle: `services/runs/steps.py` already imports domain +
  provider modules; `kdive.jobs.queue` imports domain/db only — check `just type` stays green.
  If a cycle appears, define `BootAttempt` in `services/runs/steps.py` and keep `as_data` free
  of MCP imports by returning a plain `dict[str, object]` typed loosely, OR import `JsonValue`
  lazily — prefer the direct import and only fall back if `ty`/import flags a cycle.
- The `dedup_key` literal `f"{run_id}:boot"` must match `_enqueue_step`
  (`mcp/tools/lifecycle/runs/steps.py`) exactly — `f"{run.id}:{step}"` with `step="boot"`.

**Acceptance:** all five tests pass; `just lint` + `just type` green.

### Task 2 — thread `boot_readiness` through the envelope (`mcp/tools/lifecycle/runs/common.py`)

**Failing test first** (`tests/mcp/lifecycle/test_runs_tools.py`): a unit test that calls
`envelope_for_run` directly (no transport) for a `SUCCEEDED` Run with `step_progress` showing
`boot="pending"` and a `boot_readiness=BootAttempt(...)`, asserting
`response.data["boot_readiness"] == {"job_id": ..., "status": "failed", "error_category": ...}`;
and a companion asserting that with `boot_readiness=None` there is no `boot_readiness` key.

**Implementation:** `envelope_for_run` gains keyword-only `boot_readiness: BootAttempt | None =
None`. On the `RunState.SUCCEEDED` branch (where `steps` is already built), after the existing
`steps` assignment, add:

```python
if boot_readiness is not None:
    data["boot_readiness"] = cast(JsonValue, boot_readiness.as_data())
```

- Only the `SUCCEEDED` branch sets it; `CREATED`/`RUNNING`/`CANCELED`/failed branches and every
  other caller keep the `None` default. Import `BootAttempt` from `services.runs.steps`
  (`common.py` already imports `StepProgress` from there).

**NOTE — cross-agent conflict zone:** sibling #748 also edits docstrings in `common.py`. Touch
**only** the `envelope_for_run` signature + the one `SUCCEEDED`-branch insert + the import. Do
not reflow or re-docstring anything else. Expect the orchestrator to rebase a `common.py`
conflict.

**Acceptance:** both envelope unit tests pass; `just lint` + `just type` green.

### Task 3 — call the helper on the read path (`mcp/tools/lifecycle/runs/view.py`)

**Failing test first** (`tests/mcp/lifecycle/test_runs_tools.py`, end-to-end through `get_run`
against the test DB): build a `SUCCEEDED` Run bound to a System, with a `failed` boot job
(dedup_key `f"{run_id}:boot"`, `error_category=READINESS_FAILURE`) and **no** boot `run_steps`
row (simulating the post-abandon state — seed the `failed` job via enqueue + direct
`UPDATE jobs SET state='failed', error_category=…`, as in Task 1; do not write a boot
`run_steps` row). Assert `get_run` returns `data.steps.boot == "pending"`
**and** `data.boot_readiness == {job_id, status:"failed", error_category:"readiness_failure"}`.
Companion tests: never-attempted (no boot job) → no `boot_readiness`; `queued` boot job → no
`boot_readiness`.

**Implementation:** in `get_run`, after `progress` is computed (only for `SUCCEEDED`), fetch the
attempt when boot is not yet succeeded and pass it to the envelope:

```python
boot_attempt = (
    await failed_boot_attempt(conn, run.id)
    if progress is not None and progress.boot != "succeeded"
    else None
)
```

then `envelope_for_run(run, ..., step_progress=progress, boot_readiness=boot_attempt)`.

- The fetch stays inside the `async with pool.connection() as conn:` block (it needs `conn`).
- Import `failed_boot_attempt` alongside the existing `step_progress` import in `view.py`.

**Acceptance:** the three `get_run` tests pass; `just lint` + `just type` green; full
`tests/mcp/lifecycle/test_runs_tools.py` and the `services/runs` step tests green.

### Task 4 — finalize ADR status + full guardrails

- ADR-0230 and its README row are already `Accepted` (the implementing PR ratifies them).
- Run `just lint`, `just type`, then the **full** `just test` once before pushing.
- `check-mermaid` requires `jsdom` (a node dep) that may be absent locally; it is environment
  setup, not a code gate — note in the PR body if skipped locally. CI runs it.

## Rollback / cleanup

- Pure additive read; no migration, no schema, no data write. Reverting the three source edits
  (helper, view call, envelope kwarg) fully removes the behavior. No state to clean up.
- No new dependency. No config or env var added.

## Verification gaps / risks

- The one new DB round-trip fires on every `runs.get` for a `SUCCEEDED` Run whose boot has not
  succeeded (including mid-boot). It is a single indexed lookup on the UNIQUE `dedup_key`
  column; acceptable per ADR-0230 Consequences. A booted Run and a non-`SUCCEEDED` Run pay
  nothing.
- Import-cycle risk between `services/runs/steps.py` and `kdive.jobs.queue` / `mcp.responses` —
  caught by `just type` and the test imports; Task 1 names the fallback.
