# runs.cancel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `runs.cancel(run_id)` MCP tool that drives a non-terminal Run to terminal `canceled` under the per-Run lock, best-effort cancels its in-flight build job, and frees the System without `systems.teardown`.

**Architecture:** A plain async handler `cancel_run(pool, ctx, run_id)` in a new `cancel.py`, wrapped by a thin FastMCP `runs.cancel` tool in the existing `registrar.py`. The transition uses the existing `RUNS.update_state` (which calls `ensure_transition`) under `advisory_xact_lock(LockScope.RUN, …)` — the same lock `runs.build` and the worker build handlers hold, so the cooperative build-job cancel is race-safe. No new state, no migration. See spec `docs/specs/2026-06-17-runs-cancel.md` and ADR-0157.

**Tech Stack:** Python 3.13, psycopg (async), FastMCP, pytest. Guardrails via `just` (lint/type/test). Tests are handler-direct (injected pool + `RequestContext`), no transport.

---

## Conventions every task must follow

- Read `AGENTS.md` (root) and `CLAUDE.md`. Toolchain: `uv`, `ruff`, `ty`, `pytest` via `just`.
- Absolute imports only; ≤100 lines/function; cyclomatic ≤8; 100-char lines; Google-style docstring on the public `cancel_run`.
- Guardrail before every commit: `just lint && just type && uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py -q`. Full gate `just ci` before push.
- Conventional commits, ≤72-char imperative subject, trailer `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- All test code goes in `tests/mcp/lifecycle/test_runs_tools.py`, reusing its existing helpers: `_pool(url)`, `_ctx(role)`, `_seed_run`, `_seed_running_run`, `_seed_system`, `_seed_investigation`, `_enqueue_build_job`, `_build_job_for`, `_count`, `migrated_url` fixture, `asyncio.run(_run())` wrapper. Import `cancel_run` from `kdive.mcp.tools.lifecycle.runs.cancel`.

## File structure

- **Create** `src/kdive/mcp/tools/lifecycle/runs/cancel.py` — the `cancel_run` handler (one public coroutine + small private helpers).
- **Modify** `src/kdive/mcp/tools/lifecycle/runs/registrar.py` — add `_register_runs_cancel` and call it in `register`.
- **Modify (tests)** `tests/mcp/lifecycle/test_runs_tools.py` — append the `runs.cancel` test block.

---

## Task 1: The `cancel_run` handler

**Files:**
- Create: `src/kdive/mcp/tools/lifecycle/runs/cancel.py`
- Test: `tests/mcp/lifecycle/test_runs_tools.py`

The handler contract (from the spec):

| Run state on entry | Result |
|---|---|
| bad UUID | `configuration_error` |
| no row / cross-project | `not_found` |
| caller < operator | `require_role` raises |
| `created` / `running` | transition → `canceled`; best-effort cancel `{run_id}:build` job; audit `{prior}->canceled`; success `status="canceled"` |
| already `canceled` | success no-op `status="canceled"`, no audit |
| `succeeded` / `failed` | `conflict` with `data["current_status"]` |

- [ ] **Step 1: Write the failing tests (the full block)**

Append to `tests/mcp/lifecycle/test_runs_tools.py`:

```python
# --- runs.cancel ---------------------------------------------------------------

from kdive.mcp.tools.lifecycle.runs.cancel import cancel_run  # noqa: E402


async def _run_state(pool: AsyncConnectionPool, run_id: str) -> str:
    return await _count_state(pool, run_id)


async def _count_state(pool: AsyncConnectionPool, run_id: str) -> str:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT state FROM runs WHERE id = %s", (run_id,))
        row = await cur.fetchone()
    assert row is not None
    return str(row["state"])


@pytest.mark.parametrize(
    ("state", "transition"),
    [(RunState.CREATED, "created->canceled"), (RunState.RUNNING, "running->canceled")],
)
def test_cancel_drives_non_terminal_run_canceled(
    migrated_url: str, state: RunState, transition: str
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.CREATED)
            if state is RunState.RUNNING:
                async with pool.connection() as conn:
                    await conn.execute("UPDATE runs SET state='running' WHERE id=%s", (run_id,))
            resp = await cancel_run(pool, _ctx(Role.OPERATOR), run_id)
            assert resp.status == "canceled"
            assert resp.error_category is None
            assert resp.suggested_next_actions == ["runs.create"]
            assert await _run_state(pool, run_id) == "canceled"
            n = await _count(
                pool,
                "SELECT count(*) AS n FROM audit_log WHERE transition=%s AND object_id=%s",
                (transition, run_id),
            )
        assert n == 1

    asyncio.run(_run())


def test_cancel_already_canceled_is_idempotent_no_op(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.CANCELED)
            resp = await cancel_run(pool, _ctx(Role.OPERATOR), run_id)
            assert resp.status == "canceled"
            assert resp.error_category is None
            n = await _count(
                pool,
                "SELECT count(*) AS n FROM audit_log WHERE tool='runs.cancel' AND object_id=%s",
                (run_id,),
            )
        assert n == 0

    asyncio.run(_run())


@pytest.mark.parametrize("state", [RunState.SUCCEEDED, RunState.FAILED])
def test_cancel_other_terminal_run_conflicts(migrated_url: str, state: RunState) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=state)
            resp = await cancel_run(pool, _ctx(Role.OPERATOR), run_id)
            assert resp.status == "error"
            assert resp.error_category == "conflict"
            assert resp.data["current_status"] == state.value
            assert await _run_state(pool, run_id) == state.value

    asyncio.run(_run())


def test_cancel_frees_system_for_a_new_run(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            first = await _create(pool, _ctx(), inv_id, sys_id)
            assert first.status == "created"
            blocked = await _create(pool, _ctx(), inv_id, sys_id)
            assert blocked.status == "error"
            assert blocked.error_category == "transport_conflict"
            assert blocked.data["reason"] == "system_has_live_run"
            cancel = await cancel_run(pool, _ctx(Role.OPERATOR), first.object_id)
            assert cancel.status == "canceled"
            again = await _create(pool, _ctx(), inv_id, sys_id)
            assert again.status == "created"

    asyncio.run(_run())


def test_cancel_unknown_run_id_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await cancel_run(pool, _ctx(Role.OPERATOR), str(uuid4()))
            assert resp.status == "error"
            assert resp.error_category == "not_found"

    asyncio.run(_run())


def test_cancel_malformed_run_id_is_configuration_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await cancel_run(pool, _ctx(Role.OPERATOR), "not-a-uuid")
            assert resp.status == "error"
            assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_cancel_cross_project_run_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.CREATED, project="proj")
            resp = await cancel_run(pool, _ctx(Role.OPERATOR, projects=("other",)), run_id)
            assert resp.status == "error"
            assert resp.error_category == "not_found"
            assert await _run_state(pool, run_id) == "created"

    asyncio.run(_run())


def test_cancel_requires_operator(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.CREATED)
            with pytest.raises(AuthorizationError):
                await cancel_run(pool, _ctx(Role.VIEWER), run_id)
            assert await _run_state(pool, run_id) == "created"

    asyncio.run(_run())


def test_cancel_cancels_in_flight_build_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_running_run(pool)
            await _enqueue_build_job(pool, run_id)
            resp = await cancel_run(pool, _ctx(Role.OPERATOR), run_id)
            assert resp.status == "canceled"
            async with pool.connection() as conn:
                job = await _build_job_for(conn, run_id)
            assert job.state is JobState.CANCELED
            assert await _run_state(pool, run_id) == "canceled"

    asyncio.run(_run())


def test_cancel_leaves_terminal_build_job_untouched(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_running_run(pool)
            job = await _enqueue_build_job(pool, run_id)
            async with pool.connection() as conn:
                await JOBS.update_state(conn, job.id, JobState.RUNNING)
                await JOBS.update_state(conn, job.id, JobState.SUCCEEDED)
            resp = await cancel_run(pool, _ctx(Role.OPERATOR), run_id)
            assert resp.status == "canceled"
            async with pool.connection() as conn:
                refreshed = await _build_job_for(conn, run_id)
            assert refreshed.state is JobState.SUCCEEDED

    asyncio.run(_run())


def test_cancel_running_run_with_running_build_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_running_run(pool)
            job = await _enqueue_build_job(pool, run_id)
            async with pool.connection() as conn:
                await JOBS.update_state(conn, job.id, JobState.RUNNING)
            resp = await cancel_run(pool, _ctx(Role.OPERATOR), run_id)
            assert resp.status == "canceled"
            async with pool.connection() as conn:
                refreshed = await _build_job_for(conn, run_id)
            assert refreshed.state is JobState.CANCELED
            assert await _run_state(pool, run_id) == "canceled"

    asyncio.run(_run())


def test_finalize_build_after_cancel_does_not_resurrect_run(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_running_run(pool)
            job = await _enqueue_build_job(pool, run_id)
            await cancel_run(pool, _ctx(Role.OPERATOR), run_id)
            result = BuildStepResult(kernel_ref="k", debuginfo_ref="d")
            async with pool.connection() as conn:
                run = await RUNS.get(conn, UUID(run_id))
                assert run is not None
                await runs_shared.finalize_build(conn, job, run, result)
            assert await _run_state(pool, run_id) == "canceled"

    asyncio.run(_run())
```

Add the imports this block needs near the top of the file (if not already present):
`from kdive.jobs.handlers import runs_shared`, `from kdive.services.runs.steps import BuildStepResult`. (`JOBS`, `RUNS`, `RunState`, `JobState`, `uuid4`, `UUID`, `AuthorizationError`, `dict_row` are already imported.)

> Before relying on `BuildStepResult(kernel_ref=…, debuginfo_ref=…)`, open `src/kdive/services/runs/steps.py` and confirm the constructor field names; adjust the kwargs in `test_finalize_build_after_cancel_does_not_resurrect_run` to match. If `BuildStepResult` is not trivially constructible, drop that one test and rely on `finalize_build`'s documented early-return (it is already covered by the worker's own tests) — the other ten tests are the load-bearing set.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py -k cancel -q`
Expected: collection/import error — `cannot import name 'cancel_run'` (module does not exist yet). That is the expected first failure.

- [ ] **Step 3: Write the handler**

Create `src/kdive/mcp/tools/lifecycle/runs/cancel.py`:

```python
"""`runs.cancel` MCP handler."""

from __future__ import annotations

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import JOBS, RUNS
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import Run
from kdive.domain.state import IllegalTransition, JobState, RunState
from kdive.jobs import queue
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools._common import not_found as _not_found
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role

_TERMINAL_JOB = frozenset({JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELED})
_NEXT_ACTIONS = ["runs.create"]


async def cancel_run(pool: AsyncConnectionPool, ctx: RequestContext, run_id: str) -> ToolResponse:
    """Drive a non-terminal Run to terminal ``canceled``, freeing its System (ADR-0157).

    Under the per-Run lock, transition a ``created``/``running`` Run to ``canceled`` and
    best-effort cancel its in-flight build job. A retried cancel on an already-``canceled``
    Run is an idempotent success no-op; a ``succeeded``/``failed`` Run returns ``conflict``
    (it is never relabeled). The cancel frees the System for a new ``runs.create`` with no
    ``systems.teardown``.

    Args:
        pool: The connection pool.
        ctx: The authenticated request context.
        run_id: The Run to cancel.

    Returns:
        A success envelope (``status="canceled"``) on cancel or idempotent no-op; a failure
        envelope (``not_found`` / ``configuration_error`` / ``conflict``) otherwise.
    """
    uid = _as_uuid(run_id)
    if uid is None:
        return _config_error(run_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            run = await RUNS.get(conn, uid)
            if run is None or run.project not in ctx.projects:
                return _not_found(run_id)
            require_role(ctx, run.project, Role.OPERATOR)
            return await _cancel_locked(conn, ctx, run)


async def _cancel_locked(conn: AsyncConnection, ctx: RequestContext, run: Run) -> ToolResponse:
    """Transition the Run + best-effort cancel its build job under the per-Run lock."""
    prior = run.state
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
        try:
            canceled = await RUNS.update_state(conn, run.id, RunState.CANCELED)
        except IllegalTransition:
            return await _terminal_response(conn, run)
        await _cancel_build_job_best_effort(conn, canceled.id)
        await audit.record(
            conn,
            ctx,
            audit.AuditEvent(
                tool="runs.cancel",
                object_kind="runs",
                object_id=canceled.id,
                transition=f"{prior.value}->canceled",
                args={"run_id": str(canceled.id)},
                project=canceled.project,
            ),
        )
    return _canceled_response(canceled)


async def _terminal_response(conn: AsyncConnection, run: Run) -> ToolResponse:
    """Disambiguate an already-terminal Run after `update_state` raised `IllegalTransition`.

    The re-read is fresh: `update_state` rolled back only its own inner savepoint, so the
    outer locked transaction is intact. An already-``canceled`` Run is an idempotent success;
    a ``succeeded``/``failed`` Run is a ``conflict`` naming the actual ``current_status``.
    """
    current = await RUNS.get(conn, run.id)
    state = current.state if current is not None else run.state
    if state is RunState.CANCELED:
        return _canceled_response(current or run)
    return ToolResponse.failure(
        str(run.id), ErrorCategory.CONFLICT, data={"current_status": state.value}
    )


async def _cancel_build_job_best_effort(conn: AsyncConnection, run_id: object) -> None:
    """Cancel the Run's in-flight build job if one is non-terminal; a no-op otherwise."""
    job = await queue.get_by_dedup_key(conn, f"{run_id}:build")
    if job is None or job.state in _TERMINAL_JOB:
        return
    await JOBS.update_state(conn, job.id, JobState.CANCELED)


def _canceled_response(run: Run) -> ToolResponse:
    return ToolResponse.success(
        str(run.id),
        "canceled",
        suggested_next_actions=_NEXT_ACTIONS,
        data={"project": run.project},
    )
```

> Note: `IllegalTransition` is exported from `kdive.domain.state` (confirmed: `jobs/handlers/runs.py` imports it from there). `JOBS.update_state` raising `IllegalTransition` for a job that turns terminal in the race window is not expected (we pre-check `_TERMINAL_JOB` under the per-Run lock, and a leased build job's terminal transition itself takes `LockScope.RUN` in `finalize_build`/`_fail_build`), so no extra catch is needed; if `ty`/tests reveal a path, wrap the `JOBS.update_state` in a `try/except IllegalTransition: pass` (still best-effort).

- [ ] **Step 4: Run the cancel tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py -k cancel -q`
Expected: all `*cancel*` tests PASS (plus `test_finalize_build_after_cancel_does_not_resurrect_run`).

- [ ] **Step 5: Guardrails**

Run: `just lint && just type && uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py -q`
Expected: ruff clean, ty clean, full runs-tools suite green. Fix every warning before continuing.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/mcp/tools/lifecycle/runs/cancel.py tests/mcp/lifecycle/test_runs_tools.py
git commit -m "feat: add runs.cancel handler to free a System without teardown"
```

---

## Task 2: Register the `runs.cancel` tool

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/runs/registrar.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/mcp/lifecycle/test_runs_tools.py`:

```python
def test_runs_cancel_tool_is_registered_and_mutating() -> None:
    from fastmcp import FastMCP

    from kdive.mcp.tools.lifecycle.runs import registrar as runs_registrar

    app = FastMCP("test")

    class _Pool:  # registrar only stores the pool; never connects at registration time.
        pass

    runs_registrar.register(app, _Pool(), resolver=provider_resolver())  # type: ignore[arg-type]

    async def _names() -> set[str]:
        return {t.name for t in (await app.get_tools()).values()}

    names = asyncio.run(_names())
    assert "runs.cancel" in names
```

> Before writing this test, confirm the FastMCP introspection API in this version: open another registrar test (search the test tree for `get_tools` or `list_tools`) and mirror whatever the repo already uses to enumerate registered tool names. If no such helper/test exists, replace the introspection with the simplest call the installed `fastmcp` exposes (e.g. `app._tool_manager` listing) — the assertion only needs the registered name set. Do not invent an API.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py -k runs_cancel_tool_is_registered -q`
Expected: FAIL — `runs.cancel` not in the registered names.

- [ ] **Step 3: Wire the registrar**

In `src/kdive/mcp/tools/lifecycle/runs/registrar.py`:

1. Add the import near the other handler imports:

```python
from kdive.mcp.tools.lifecycle.runs.cancel import cancel_run as _cancel_run
```

2. Add the registration call in `register(...)`, after `_register_runs_create(app, pool)`:

```python
    _register_runs_cancel(app, pool)
```

3. Add the registrar function (mirror `_register_runs_install`, no resolver):

```python
def _register_runs_cancel(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="runs.cancel",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def runs_cancel(
        run_id: Annotated[str, Field(description="The non-terminal Run to cancel.")],
    ) -> ToolResponse:
        """Cancel a non-terminal run, freeing its system without a teardown."""
        return await _cancel_run(pool, current_context(), run_id)
```

- [ ] **Step 4: Run it to verify it passes**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py -k runs_cancel_tool_is_registered -q`
Expected: PASS.

- [ ] **Step 5: Guardrails**

Run: `just lint && just type && uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py -q`
Expected: all green, zero warnings.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/mcp/tools/lifecycle/runs/registrar.py tests/mcp/lifecycle/test_runs_tools.py
git commit -m "feat: register the runs.cancel MCP tool"
```

---

## Task 3: Full gate + ADR/spec status

**Files:**
- Modify: `docs/adr/0157-runs-cancel-tool.md`, `docs/adr/README.md` (status `Proposed` → `Accepted` is done by the merging PR per the ADR process; leave `Proposed` until merge).

- [ ] **Step 1: Run the full CI gate**

Run: `just ci`
Expected: every recipe green (lint, type, lock-check, lint-shell, lint-workflows, check-mermaid, docs-*, adr-status-check, …, test). If a doc recipe needs GNU bash (`mapfile`) and the host has only bash 3.2, that recipe is environmental — note it; CI runs it on Linux. Fix any real failure before push.

- [ ] **Step 2: No commit unless a fix was needed.** If `just ci` required a change, commit it with a conventional subject. Otherwise proceed to the branch review.

---

## Self-review checklist (run after implementation, before the branch review)

1. **Spec coverage** — every spec success criterion (1–8, 5a) maps to a test in Task 1/2. The `system_has_live_run` "cancel frees the System" criterion (spec #4) is the real `runs.create` round-trip in `test_cancel_frees_system_for_a_new_run`, using the existing `_create` / `_seed_system` / `_seed_investigation` helpers (create #1 → create #2 blocked `system_has_live_run` → cancel #1 → create #3 succeeds).
2. **Placeholder scan** — no TBD/TODO; every code step shows real code.
3. **Type consistency** — `cancel_run`, `_cancel_locked`, `_terminal_response`, `_cancel_build_job_best_effort`, `_canceled_response` names are used consistently; `_NEXT_ACTIONS == ["runs.create"]` matches the envelope assertions.
