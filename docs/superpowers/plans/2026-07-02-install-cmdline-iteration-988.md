# Install-time boot-cmdline iteration Implementation Plan (#988)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an agent apply a fresh kernel cmdline against an already-built kernel through `runs.install` and boot it, so boot-parameter variants can be swept without a rebuild (#988).

**Architecture:** Relocate the cmdline entry point from the build step to the install step. The `run_steps` ledger is authoritative; the job dedup is subordinate. A cmdline that differs from the one recorded on the `install` step recycles the `install`+`boot` ledger rows under the per-Run lock; the install/boot enqueue is generalized so an absent ledger row recycles the terminal (succeeded-or-failed) job **payload-and-all**, letting the existing flow re-run with the new cmdline.

**Tech Stack:** Python 3.14, `uv`, `psycopg` (async), pydantic payloads, FastMCP tool wrappers, pytest. Spec: `docs/superpowers/specs/2026-07-02-install-cmdline-iteration-988.md`; decision: `docs/adr/0299-install-cmdline-iteration.md`.

## Global Constraints

- Guardrails: `just lint` (ruff check + format), `just type` (`ty`, whole tree src+tests), `just test` (suite minus `live_vm`). CI runs these individually. Run a single test with `uv run python -m pytest <path>::<name> -q`.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. Google-style docstrings on non-trivial public APIs. Absolute imports only.
- Doc-style: no "critical/crucial/essential/significant/comprehensive/robust/elegant", never "Sprint". Never leak `ADR-NNNN` into agent-facing tool text (`test_no_adr_leak` guard).
- The **wrapper** docstring + `Field(description=...)` is the agent-facing contract — update the `@app.tool` wrapper, not only the inner handler (`test_read_tools_annotated`, agent-doc guards).
- Error taxonomy: reuse `ErrorCategory.CONFIGURATION_ERROR`; never invent categories. `data.reason` strings: `cmdline_overrides_platform_args`, `cmdline_blank`, `step_in_progress`.
- No schema/migration, no new `JobKind`, no RBAC/role change, no new config.
- Commit after each task with the `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.

---

## File structure

- `src/kdive/jobs/payloads.py` — add `InstallPayload(RunPayload)`; map `JobKind.INSTALL → InstallPayload` in `_PAYLOAD_MODELS`, `_RUN_PAYLOAD_MODELS`, and the `_PayloadModel` union. (Task 1)
- `src/kdive/services/runs/steps.py` — `cmdline_for` gains `override`; `StepProgress` gains `installed_cmdline`; `step_progress` reads the `install` result; add `delete_run_step` reader helpers. (Tasks 2, 5, 7)
- `src/kdive/jobs/queue.py` — generalize `retry_terminal_failed` → `recycle_terminal` (succeeded-or-failed, overwrite payload, clear `result_ref`). (Task 3)
- `src/kdive/mcp/tools/lifecycle/runs/steps.py` — `install_run` gains `cmdline`, guards, and the re-stage decision; `_enqueue_step` gains `payload`/`recycle`. (Task 4)
- `src/kdive/jobs/handlers/runs/install.py` — load `InstallPayload`, pass `override`, record applied extra. (Task 5)
- `src/kdive/mcp/tools/lifecycle/runs/common.py` + `.../runs/view.py` — surface `data.installed_cmdline`. (Task 6)
- `src/kdive/mcp/tools/lifecycle/runs/registrar.py` — `runs.install` `cmdline` Field; `runs.boot` docstring. (Task 7)
- `tests/…` — one test module per task (paths named inline).
- `tests/live/…` — gated acceptance sweep. (Task 8)

---

## Task 1: `InstallPayload` carries the install cmdline

**Files:**
- Modify: `src/kdive/jobs/payloads.py` (add class near `BuildPayload` ~line 84; edit `_PayloadModel` union ~line 195, `_PAYLOAD_MODELS` line 206, `_RUN_PAYLOAD_MODELS` line 221)
- Test: `tests/jobs/test_payloads.py`

**Interfaces:**
- Produces: `class InstallPayload(RunPayload)` with `cmdline: str | None = None` and a `_nonblank_cmdline` validator (strip; blank → `ValueError`). `JobKind.INSTALL` now dispatches to `InstallPayload`.

- [ ] **Step 1: Write the failing test**

```python
# tests/jobs/test_payloads.py
import pytest
from kdive.domain.operations.jobs import JobKind
from kdive.jobs.payloads import InstallPayload, PayloadValidationError, dump_payload

_RID = "11111111-1111-1111-1111-111111111111"

def test_install_payload_round_trips_cmdline():
    dumped = dump_payload(JobKind.INSTALL, InstallPayload(run_id=_RID, cmdline="  dhash_entries=1 "))
    assert dumped == {"run_id": _RID, "cmdline": "dhash_entries=1"}  # stripped

def test_install_payload_omits_absent_cmdline():
    dumped = dump_payload(JobKind.INSTALL, InstallPayload(run_id=_RID))
    assert dumped == {"run_id": _RID}  # exclude_none drops cmdline

def test_install_payload_rejects_blank_cmdline():
    with pytest.raises(ValueError):
        InstallPayload(run_id=_RID, cmdline="   ")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/jobs/test_payloads.py::test_install_payload_round_trips_cmdline -q`
Expected: FAIL — `ImportError: cannot import name 'InstallPayload'`.

- [ ] **Step 3: Implement**

```python
# src/kdive/jobs/payloads.py — after BuildInstallBootPayload (mirror BuildPayload's validator)
class InstallPayload(RunPayload):
    """Payload for a `runs.install` step: the Run plus an optional cmdline override (#988, ADR-0299).

    ``cmdline`` replaces the build-baked extra args for this install; ``None`` reuses them. Blank is
    rejected (a caller mistake, distinct from omitting the argument).
    """

    cmdline: str | None = None

    @field_validator("cmdline")
    @classmethod
    def _nonblank_cmdline(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("cmdline must not be blank")
        return stripped
```

Add `| InstallPayload` to the `_PayloadModel` union, and set `JobKind.INSTALL: InstallPayload` in both `_PAYLOAD_MODELS` and `_RUN_PAYLOAD_MODELS`.

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/jobs/test_payloads.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/jobs/payloads.py tests/jobs/test_payloads.py
git commit -m "feat(988): add InstallPayload carrying the install cmdline"
```

---

## Task 2: `cmdline_for` override replaces the build-baked extra

**Files:**
- Modify: `src/kdive/services/runs/steps.py:337-344` (`cmdline_for`)
- Test: `tests/services/runs/test_cmdline.py`

**Interfaces:**
- Consumes: `system_required_cmdline(method, root_cmdline)` (unchanged).
- Produces: `async def cmdline_for(conn, run, method, *, root_cmdline, override: str | None = None) -> str`. `override` set → `f"{required} {override.strip()}"` (replace); `override` `None` → today's build-baked append.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/runs/test_cmdline.py — extend
import pytest
from kdive.domain.capture import CaptureMethod
from kdive.services.runs.steps import cmdline_for

@pytest.mark.asyncio
async def test_cmdline_for_override_replaces_build_extra(build_result_conn):
    # build_result_conn: a conn whose run has build result cmdline="dhash_entries=9"
    conn, run = build_result_conn
    out = await cmdline_for(conn, run, CaptureMethod.HOST_DUMP, root_cmdline="root=/dev/vda",
                            override="dhash_entries=1")
    assert out == "console=ttyS0 root=/dev/vda dhash_entries=1"  # 9 replaced by 1

@pytest.mark.asyncio
async def test_cmdline_for_no_override_uses_build_extra(build_result_conn):
    conn, run = build_result_conn
    out = await cmdline_for(conn, run, CaptureMethod.HOST_DUMP, root_cmdline="root=/dev/vda")
    assert out == "console=ttyS0 root=/dev/vda dhash_entries=9"
```

(If a DB fixture is heavy, prefer a focused unit test that stubs `existing_build_result`; match the existing style in `test_cmdline.py`.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/services/runs/test_cmdline.py::test_cmdline_for_override_replaces_build_extra -q`
Expected: FAIL — `cmdline_for() got an unexpected keyword argument 'override'`.

- [ ] **Step 3: Implement**

```python
# src/kdive/services/runs/steps.py
async def cmdline_for(
    conn: AsyncConnection, run: Run, method: CaptureMethod, *, root_cmdline: str | None,
    override: str | None = None,
) -> str:
    """Compose the boot cmdline (ADR-0183, ADR-0299).

    ``override`` (the ``runs.install`` cmdline, #988) **replaces** the build-baked extra; when
    ``None`` the build step's recorded extra is appended (unchanged). Platform tokens
    (``system_required_cmdline``) always lead and are never modifiable.
    """
    required = system_required_cmdline(method, root_cmdline)
    if override is not None:
        return f"{required} {override.strip()}"
    result = await existing_build_result(conn, run.id)
    if result is not None and result.cmdline is not None and result.cmdline.strip():
        return f"{required} {result.cmdline.strip()}"
    return required
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/services/runs/test_cmdline.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/services/runs/steps.py tests/services/runs/test_cmdline.py
git commit -m "feat(988): cmdline_for override replaces the build-baked extra"
```

---

## Task 3: Payload-carrying, terminal (succeeded-or-failed) job recycle

**Files:**
- Modify: `src/kdive/jobs/queue.py:36-94` (`enqueue`)
- Modify caller: `src/kdive/mcp/tools/lifecycle/runs/steps.py:135` (flag rename only; behavior in Task 4)
- Test: `tests/jobs/test_queue.py`

**Interfaces:**
- Produces: `enqueue(..., *, recycle_terminal: bool = False)` replacing `retry_terminal_failed`. When set, a `failed` **or** `succeeded` job for `dedup_key` is reset to `queued` with `attempt=0`, `worker_id/lease/heartbeat/error_category/failure_context` cleared, **`payload` overwritten with the new payload**, and **`result_ref = NULL`**. `queued`/`running`/`canceled` untouched.

- [ ] **Step 1: Write the failing test**

```python
# tests/jobs/test_queue.py — extend (mirror existing enqueue tests / fixtures)
@pytest.mark.asyncio
async def test_recycle_terminal_resets_succeeded_job_with_new_payload(queue_conn):
    conn = queue_conn
    j1 = await enqueue(conn, JobKind.INSTALL, InstallPayload(run_id=_RID, cmdline="a=1"),
                       _authorizing(), f"{_RID}:install")
    await mark_succeeded(conn, j1.id, result_ref="old-result")  # helper: state=succeeded + result_ref
    j2 = await enqueue(conn, JobKind.INSTALL, InstallPayload(run_id=_RID, cmdline="a=2"),
                       _authorizing(), f"{_RID}:install", recycle_terminal=True)
    assert j2.id == j1.id
    assert j2.state is JobState.QUEUED
    assert j2.payload["cmdline"] == "a=2"        # payload overwritten
    assert j2.result_ref is None                 # success field cleared

@pytest.mark.asyncio
async def test_recycle_terminal_leaves_running_job_untouched(queue_conn):
    conn = queue_conn
    j1 = await enqueue(conn, JobKind.INSTALL, InstallPayload(run_id=_RID), _authorizing(), f"{_RID}:install")
    await mark_running(conn, j1.id)
    j2 = await enqueue(conn, JobKind.INSTALL, InstallPayload(run_id=_RID, cmdline="a=2"),
                       _authorizing(), f"{_RID}:install", recycle_terminal=True)
    assert j2.state is JobState.RUNNING           # in-flight not resurrected
    assert "cmdline" not in j2.payload            # payload untouched
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/jobs/test_queue.py::test_recycle_terminal_resets_succeeded_job_with_new_payload -q`
Expected: FAIL — `enqueue() got an unexpected keyword argument 'recycle_terminal'` (or payload not overwritten).

- [ ] **Step 3: Implement**

```python
# src/kdive/jobs/queue.py — signature: replace retry_terminal_failed with recycle_terminal
    recycle_terminal: bool = False,
...
        if recycle_terminal:
            await cur.execute(
                "UPDATE jobs SET state = %s, payload = %s, attempt = 0, worker_id = NULL, "
                "    lease_expires_at = NULL, heartbeat_at = NULL, error_category = NULL, "
                "    result_ref = NULL, failure_context = '{}'::jsonb "
                "WHERE dedup_key = %s AND state = ANY(%s)",
                (
                    JobState.QUEUED.value,
                    Jsonb(payload_json),
                    dedup_key,
                    [JobState.FAILED.value, JobState.SUCCEEDED.value],
                ),
            )
```

Update the docstring: the fence now recycles `failed`/`succeeded` in place, overwriting the payload and clearing `result_ref`, so a re-staged install carries its new cmdline (ADR-0299); `queued`/`running`/`canceled` stay untouched. In `mcp/tools/lifecycle/runs/steps.py:135`, rename the kwarg to `recycle_terminal=True` for now (Task 4 makes it ledger-driven).

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/jobs/test_queue.py tests/adversarial/test_queue_concurrency.py -q`
Expected: PASS (existing failed-retry tests still green — a failed job is a subset of the new fence).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/jobs/queue.py src/kdive/mcp/tools/lifecycle/runs/steps.py tests/jobs/test_queue.py
git commit -m "feat(988): recycle terminal jobs payload-and-all, clearing result_ref"
```

---

## Task 4: Re-stage decision in `runs.install` (tool boundary)

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/runs/steps.py` (`install_run`, `_enqueue_step`)
- Add reader/deleter: `src/kdive/services/runs/steps.py` (`installed_cmdline`, `delete_run_step`) — or reuse `step_progress` (Task 5 adds `installed_cmdline` to it)
- Test: `tests/mcp/lifecycle/test_runs_tools.py`

**Interfaces:**
- Consumes: `platform_owned_cmdline_token` (steps.py), `step_progress` (install/boot state + `installed_cmdline`, Task 5), `queue.enqueue(recycle_terminal=…)` (Task 3), `InstallPayload` (Task 1).
- Produces: `async def install_run(pool, ctx, run_id, *, cmdline: str | None = None, idempotency_key=None)`. Enqueues `InstallPayload(run_id, cmdline)`. `_enqueue_step(conn, ctx, run, kind, step, tool, *, payload, recycle: bool)`.

**Decision (under the per-Run advisory lock), from `step_progress(conn, run.id)`:**
1. `cmdline` guards run first (before the lock): platform-owned token → `cmdline_overrides_platform_args`; blank → `cmdline_blank`.
2. `requested = cmdline.strip() if cmdline else installed_extra_from_build` — but the compare uses the **install-recorded** extra; simplest: `requested_norm = cmdline.strip() if cmdline else <build-baked extra>`. Compute the build-baked extra via `existing_build_result`.
3. install step `running` **or** boot step `running` → `step_in_progress`.
4. install step `succeeded` and `installed_cmdline == requested_norm` → no-op: enqueue with `recycle=False` (returns the existing succeeded job envelope).
5. install step `succeeded` and differs → re-stage: `delete_run_step(install)`, `delete_run_step(boot)`, enqueue with `recycle=True`.
6. install step `pending` → first install: enqueue with `recycle=False` (no prior job) — a plain insert.

- [ ] **Step 1: Write the failing tests**

```python
# tests/mcp/lifecycle/test_runs_tools.py — extend (reuse the file's Run/System/build fixtures)
@pytest.mark.asyncio
async def test_install_rejects_platform_owned_cmdline(runs_env):
    resp = await install_run(pool, ctx, run_id, cmdline="root=/dev/sda1")
    assert resp.error_category is ErrorCategory.CONFIGURATION_ERROR
    assert resp.data["reason"] == "cmdline_overrides_platform_args"
    assert resp.data["token"] == "root="

@pytest.mark.asyncio
async def test_install_rejects_blank_cmdline(runs_env):
    resp = await install_run(pool, ctx, run_id, cmdline="   ")
    assert resp.data["reason"] == "cmdline_blank"

@pytest.mark.asyncio
async def test_install_enqueues_install_payload_with_cmdline(runs_env):
    resp = await install_run(pool, ctx, run_id, cmdline="dhash_entries=1")
    job = await queue.get_by_dedup_key(conn, f"{run_id}:install")
    assert job.payload["cmdline"] == "dhash_entries=1"

@pytest.mark.asyncio
async def test_install_differing_cmdline_restages_install_and_boot(runs_env_installed_booted):
    # install step succeeded with recorded cmdline "dhash_entries=1"; boot succeeded
    resp = await install_run(pool, ctx, run_id, cmdline="dhash_entries=2")
    assert resp.error_category is None
    # install + boot ledger rows deleted, fresh install job queued with new payload
    assert await _row_absent(conn, run_id, "boot")
    job = await queue.get_by_dedup_key(conn, f"{run_id}:install")
    assert job.state is JobState.QUEUED and job.payload["cmdline"] == "dhash_entries=2"

@pytest.mark.asyncio
async def test_install_same_cmdline_is_noop(runs_env_installed_booted):
    resp = await install_run(pool, ctx, run_id, cmdline="dhash_entries=1")  # equals recorded
    assert await _row_present(conn, run_id, "boot")  # boot NOT recycled

@pytest.mark.asyncio
async def test_install_rejected_while_boot_running(runs_env_boot_running):
    resp = await install_run(pool, ctx, run_id, cmdline="dhash_entries=2")
    assert resp.data["reason"] == "step_in_progress"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py -k "cmdline or restage or step_in_progress or noop" -q`
Expected: FAIL — `install_run() got an unexpected keyword argument 'cmdline'`.

- [ ] **Step 3: Implement**

Add `delete_run_step` to `services/runs/steps.py` (or `db/idempotency.py` beside `abandon_run_step`):

```python
async def delete_run_step(conn: AsyncConnection, run_id: UUID, step: str) -> None:
    """Delete a run step row regardless of state, to recycle a settled step (ADR-0299).

    Distinct from ``abandon_run_step`` (RUNNING-only): re-stage deletes a ``succeeded`` row so the
    step re-runs. The caller holds the per-Run advisory lock and has already verified the step is
    not RUNNING, so no worker is mid-flight on it.
    """
    async with conn.transaction():
        await conn.execute("DELETE FROM run_steps WHERE run_id = %s AND step = %s", (run_id, step))
```

Rewrite `install_run` to add `cmdline`, run the guards, compute `requested_norm`, take the lock, read `step_progress`, apply the decision table, and enqueue `InstallPayload` via a `_enqueue_step` that now takes `payload` and `recycle`. `boot_run` keeps `RunPayload` and passes `recycle = (boot ledger row absent)`. Guard helper (reuse the build path's):

```python
owned = platform_owned_cmdline_token(cmdline)
if owned is not None:
    return _config_error(run_id, data={"reason": "cmdline_overrides_platform_args", "token": owned})
if cmdline is not None and not cmdline.strip():
    return _config_error(run_id, data={"reason": "cmdline_blank"})
```

`_enqueue_step` builds the payload passed in and calls `queue.enqueue(..., recycle_terminal=recycle)`. For install, `recycle` is `True` on the re-stage branch (after deleting the rows) and `False` otherwise; for boot, `recycle = not await _has_step_row(conn, run_id, "boot")`.

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/lifecycle/runs/steps.py src/kdive/services/runs/steps.py tests/mcp/lifecycle/test_runs_tools.py
git commit -m "feat(988): re-stage install on a differing cmdline; guards + boot recycle"
```

---

## Task 5: Install handler applies the override and records the applied extra

**Files:**
- Modify: `src/kdive/jobs/handlers/runs/install.py:33-100`
- Modify: `src/kdive/services/runs/steps.py` (`StepProgress` + `step_progress` read the `install` result's `cmdline`)
- Test: `tests/jobs/handlers/test_runs_install.py`, `tests/services/runs/test_steps.py`

**Interfaces:**
- Consumes: `InstallPayload` (Task 1), `cmdline_for(override=…)` (Task 2).
- Produces: install step result gains `cmdline` (the applied client extra, already-normalized). `StepProgress.installed_cmdline: str | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/jobs/handlers/test_runs_install.py — extend
@pytest.mark.asyncio
async def test_install_handler_uses_payload_cmdline_override(install_env):
    job = _install_job(run_id, cmdline="dhash_entries=1")
    await install_handler(conn, job, resolver=resolver)
    assert install_env.captured_request.cmdline.endswith("dhash_entries=1")
    assert install_env.captured_request.cmdline.count("dhash_entries") == 1  # replaced, not appended
    row = await _install_step_result(conn, run_id)
    assert row["cmdline"] == "dhash_entries=1"  # recorded applied extra
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/jobs/handlers/test_runs_install.py::test_install_handler_uses_payload_cmdline_override -q`
Expected: FAIL — handler loads `RunPayload` (no `cmdline`) / result has no `cmdline`.

- [ ] **Step 3: Implement**

In `install_handler`: `payload = load_payload(job, InstallPayload)`; `override = payload.cmdline`; `cmdline = await cmdline_for(conn, run, method, root_cmdline=runtime.platform_root_cmdline, override=override)`; record the applied extra in the completed step:

```python
applied_extra = override if override is not None else (
    (await existing_build_result(conn, run_id)).cmdline if ... else None
)
await complete_run_step(conn, run_id, "install", {"system_id": str(system_id), "cmdline": applied_extra})
```

(Keep `applied_extra` already-normalized; `InstallPayload` strips `override`, and the build-baked extra is stored stripped.) Add `installed_cmdline` to `StepProgress` and read it in `step_progress` from the `install` row's `result["cmdline"]` (mirror how the `boot` row is read at steps.py:208-217).

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/jobs/handlers/test_runs_install.py tests/services/runs/test_steps.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/jobs/handlers/runs/install.py src/kdive/services/runs/steps.py tests/jobs/handlers/test_runs_install.py tests/services/runs/test_steps.py
git commit -m "feat(988): install handler applies the cmdline override and records it"
```

---

## Task 6: `runs.get` surfaces `data.installed_cmdline`

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/runs/common.py:174-179` (add `installed_cmdline` to the data map) and `envelope_for_run`
- Modify: `src/kdive/mcp/tools/lifecycle/runs/view.py:79-88` (pass `step_progress.installed_cmdline`)
- Test: `tests/mcp/lifecycle/test_runs_tools.py`

**Interfaces:**
- Consumes: `StepProgress.installed_cmdline` (Task 5).
- Produces: `runs.get` `data.installed_cmdline` (`str | None`).

- [ ] **Step 1: Write the failing test**

```python
# tests/mcp/lifecycle/test_runs_tools.py — extend
@pytest.mark.asyncio
async def test_runs_get_surfaces_installed_cmdline(runs_env_installed):
    resp = await get_run(pool, ctx, run_id, resolver=resolver)  # install applied "dhash_entries=1"
    assert resp.data["installed_cmdline"] == "dhash_entries=1"

@pytest.mark.asyncio
async def test_runs_get_installed_cmdline_null_before_install(runs_env_built):
    resp = await get_run(pool, ctx, run_id, resolver=resolver)
    assert resp.data.get("installed_cmdline") is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py::test_runs_get_surfaces_installed_cmdline -q`
Expected: FAIL — `KeyError: 'installed_cmdline'`.

- [ ] **Step 3: Implement**

Thread `installed_cmdline` through `envelope_for_run(..., installed_cmdline: str | None = None)` and emit `data["installed_cmdline"] = installed_cmdline` when the run is `SUCCEEDED` (from `step_progress.installed_cmdline`). Emit the key unconditionally on a booted/installed run (value may be `None`). Keep the `_required_cmdline_data` comment accurate: extra args now come from `runs.install.cmdline` too.

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/lifecycle/runs/common.py src/kdive/mcp/tools/lifecycle/runs/view.py tests/mcp/lifecycle/test_runs_tools.py
git commit -m "feat(988): surface installed_cmdline on runs.get for sweep read-back"
```

---

## Task 7: Agent-facing wrapper — `runs.install` cmdline Field + `runs.boot` doc

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/runs/registrar.py:530-566` (`runs_install`, `runs_boot`)
- Test: `tests/mcp/test_read_tools_annotated.py` (or the agent-doc guard test that reads the schema)

**Interfaces:**
- Produces: `runs.install(run_id, cmdline=None, idempotency_key=None)`; the `cmdline` `Field` enumerates the always-present, never-modifiable platform tokens.

- [ ] **Step 1: Write the failing test**

```python
# tests/mcp/lifecycle/test_runs_tools.py or test_read_tools_annotated.py
def test_runs_install_cmdline_field_names_platform_tokens(tool_schema):
    field = tool_schema("runs.install")["properties"]["cmdline"]["description"]
    for token in ("console=ttyS0", "root=/dev/vda", "crashkernel=256M", "nokaslr"):
        assert token in field
    assert "replace" in field.lower()

def test_runs_boot_doc_points_to_install_for_iteration(tool_schema):
    doc = tool_schema("runs.boot")["description"] if ... else runs_boot_docstring()
    assert "fixed at build time" not in doc
    assert "runs.install" in doc
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py -k "cmdline_field or points_to_install" -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

Add the `cmdline` param to `runs_install` and thread it into `_install_run(..., cmdline=cmdline, ...)`. Field text (no `ADR-NNNN`, no banned words):

```
Kernel debug args applied against the already-built kernel — no rebuild needed. Replaces
any build-time extra args. These platform args are always present and cannot be
overridden: console=ttyS0, root=/dev/vda, plus crashkernel=256M (kdump) or nokaslr
(gdbstub) per the System's capture method. Passing a value different from the currently
installed one re-stages the boot; sweep boot-parameter variants by calling runs.install
with a new value then runs.boot, using a distinct (or no) idempotency_key each time. Omit
to reuse the build-time cmdline.
```

Rewrite the `runs.boot` docstring to remove "fixed at build time" and point cmdline iteration at `runs.install`.

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py tests/mcp/test_read_tools_annotated.py -q` and the ADR-leak guard: `uv run python -m pytest -k no_adr_leak -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/lifecycle/runs/registrar.py tests/
git commit -m "feat(988): expose runs.install cmdline; retarget runs.boot doc"
```

---

## Task 8: Gated live acceptance sweep (`live_vm`)

**Files:**
- Create/extend: a `live_vm`-marked test under `tests/` (mirror an existing `live_vm` install/boot exercise)
- Test: the new gated test only

**Interfaces:** end-to-end over the real provider; not a PR gate.

- [ ] **Step 1: Write the gated test**

```python
@pytest.mark.live_vm
@pytest.mark.asyncio
async def test_sweep_two_cmdlines_no_rebuild(live_stack):
    # build once, then install(dhash_entries=1)->boot, install(dhash_entries=2)->boot
    build_count_before = await _build_step_count(run_id)
    await _install_and_boot(run_id, cmdline="dhash_entries=1")
    line1 = await _console_cmdline(system_id)  # /proc/cmdline or console banner
    await _install_and_boot(run_id, cmdline="dhash_entries=2")
    line2 = await _console_cmdline(system_id)
    assert "dhash_entries=1" in line1 and "dhash_entries=2" in line2
    assert await _build_step_count(run_id) == build_count_before  # no rebuild
```

- [ ] **Step 2: Verify it is collected but skipped by default**

Run: `uv run python -m pytest <path>::test_sweep_two_cmdlines_no_rebuild -q`
Expected: SKIP (`live_vm` marker) on a non-KVM runner. On the KVM host: `just test-live` runs it (operator step, not part of CI gate — see [host-runs-live-vm-tests] memory).

- [ ] **Step 3: Commit**

```bash
git add tests/
git commit -m "test(988): gated live sweep — two cmdlines, one build"
```

---

## Final verification (before PR)

- [ ] `just lint` — clean.
- [ ] `just type` — clean (whole tree).
- [ ] `just test` — full suite green (excludes `live_vm`).
- [ ] Manually confirm the ADR-leak and agent-doc guards pass: `uv run python -m pytest -k "adr_leak or read_tools_annotated" -q`.

## Self-review notes (spec coverage)

- Replace semantics → Task 2. Re-stage state machine (equal/differ/running/first) → Task 4. Payload-carrying recycle + `result_ref` clear → Task 3. Read-back → Tasks 5–6. Field enumeration of platform tokens → Task 7. Guards (`cmdline_overrides_platform_args`/`cmdline_blank`/`step_in_progress`) → Task 4. Normalization pinned (strip) → Tasks 1, 2, 5. Composite/remote untouched → no task needed (verified in spec). Acceptance sweep → Task 8.
- Type consistency: `InstallPayload.cmdline` (Task 1) ↔ `cmdline_for(override=...)` (Task 2) ↔ `install_handler` override (Task 5) ↔ `StepProgress.installed_cmdline` (Task 5) ↔ `envelope_for_run(installed_cmdline=...)` (Task 6). `recycle_terminal` kwarg (Task 3) ↔ `_enqueue_step(recycle=...)` caller (Task 4).
