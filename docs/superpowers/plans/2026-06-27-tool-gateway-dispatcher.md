# Tool Gateway (1b dispatcher) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a 1b tool gateway to the kdive MCP server — `tools.invoke` dispatcher, `tools.search` discovery, a `runs.build_install_boot` composite worker job, and an off-by-default core-set listing filter — so capability is preserved while the default catalog can shrink.

**Architecture:** `tools.invoke` re-enters `app.call_tool(name, args, run_middleware=True)` so the inner tool runs the full middleware stack natively. The composite is a new `JobKind` whose worker handler calls the existing per-phase executors (`build_handler`/`install_handler`/`boot_handler`) in sequence. A `CORE_TOOLS` set intersects `ToolExposureMiddleware.on_list_tools` only when `KDIVE_MCP_TOOL_GATEWAY` is on (default off). Per-call recording middleware skip the two meta-tools so the re-entered inner call is the sole recorder.

**Tech Stack:** Python 3.14, `uv`, FastMCP 3.4.2, psycopg, Postgres, pytest. Guardrails: `just lint`, `just type`, `just test` (CI runs each individually — see [[ci-runs-justfile-recipes-individually]]).

## Global Constraints

- ADR: `docs/adr/0268-tool-gateway-dispatcher.md`. Spec: `docs/specs/2026-06-27-tool-gateway-dispatcher-866.md`. Both are authoritative; do not contradict them.
- `KDIVE_MCP_TOOL_GATEWAY` ships **off by default**. The catalog reduction is inert until an operator sets it on. The default flip is a *separate follow-up PR*, not this one.
- The gateway is **not a security control**. Execution-time `require_role` / the destructive-op gate remain the only boundary (ADR-0148). The listing filter fails **open** (show the full catalog) on any error — never fail closed.
- New tool names: `tools.invoke`, `tools.search`. New `JobKind`: `build_install_boot`. New migration: `0051`.
- `_PayloadBase` is `extra="forbid"` — payloads cannot carry foreign fields.
- Migrations are additive and forward-only (ADR-0015). Constraint `jobs_kind_check` keeps its name across a drop-and-recreate (SQL↔enum tie tested in `tests/.../test_migrate.py`).
- Run guardrails before every commit: `just lint && just type` plus the focused test for the task. Commit messages use Conventional Commits and end with the trailer `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Line length 100; absolute imports only; Google-style docstrings on new public functions.

---

## File Structure

- `src/kdive/domain/operations/jobs.py` — add `JobKind.BUILD_INSTALL_BOOT` (Task 1).
- `src/kdive/db/schema/0051_build_install_boot_job_kind.sql` — widen `jobs_kind_check` (Task 1).
- `src/kdive/jobs/payloads.py` — `BuildInstallBootPayload` + register in the loader map (Task 2).
- `src/kdive/jobs/handlers/runs/composite.py` — the composite handler (Task 2).
- `src/kdive/jobs/handlers/runs/registrar.py` — register the composite handler (Task 2).
- `src/kdive/mcp/tools/lifecycle/runs/composite.py` — `runs.build_install_boot` admission + enqueue (Task 3).
- `src/kdive/mcp/tools/lifecycle/runs/registrar.py` — register the composite tool (Task 3).
- `src/kdive/mcp/tools/gateway.py` — `tools.invoke` + `tools.search` registrar (Tasks 4–5).
- `src/kdive/mcp/tool_index.py` — curated keywords + namespace TOC (Task 5, Task 8).
- `src/kdive/mcp/tool_registration.py` — register the gateway plane (Task 4).
- `src/kdive/mcp/middleware/{usage,telemetry,denial_audit}.py` — meta-tool skip-set (Task 6).
- `src/kdive/mcp/middleware/shared.py` — shared `META_TOOLS` constant (Task 6).
- `src/kdive/mcp/exposure.py` — `CORE_TOOLS`, classify the 3 new tools (Task 7, Task 9).
- `src/kdive/mcp/middleware/exposure.py` — chain `CORE_TOOLS` + config flag into `on_list_tools` (Task 7).
- `src/kdive/mcp/app.py` — pass `instructions` to `FastMCP(...)` (Task 8).
- Tests colocated under `tests/` mirroring the above.

---

## Task 1: New `JobKind.BUILD_INSTALL_BOOT` + migration 0051

**Files:**
- Modify: `src/kdive/domain/operations/jobs.py` (the `JobKind` enum)
- Create: `src/kdive/db/schema/0051_build_install_boot_job_kind.sql`
- Test: the existing migration/enum-tie test (locate with `rg -l "jobs_kind_check" tests`)

**Interfaces:**
- Produces: `JobKind.BUILD_INSTALL_BOOT` (value `"build_install_boot"`), consumed by Tasks 2 and 3.

- [ ] **Step 1: Find the enum-tie test and read what it asserts**

Run: `rg -n "jobs_kind_check|JobKind" tests --no-heading -l` and open the file (it asserts the SQL CHECK set equals the `JobKind` enum values). This is the failing test target.

- [ ] **Step 2: Add the enum member (test will now expect the SQL to match)**

In `src/kdive/domain/operations/jobs.py`, add to `JobKind` after `CAPTURE_VMCORE`:

```python
    BUILD_INSTALL_BOOT = "build_install_boot"
```

- [ ] **Step 3: Run the enum-tie test to verify it fails**

Run: `uv run python -m pytest <enum-tie test path> -v`
Expected: FAIL — SQL `jobs_kind_check` lacks `build_install_boot`.

- [ ] **Step 4: Add migration 0051**

Create `src/kdive/db/schema/0051_build_install_boot_job_kind.sql`, modelled exactly on `0040_diagnostics_worker_check_job_kind.sql`:

```sql
-- 0051_build_install_boot_job_kind.sql — composite build->install->boot job (ADR-0268, #866).
-- Additive to 0003/0024/0040 (forward-only, ADR-0015). Widens the jobs.kind CHECK to admit the
-- `build_install_boot` composite op (runs.build_install_boot enqueues one job whose handler runs
-- the three phases). Drop-and-recreate keeps the constraint name stable for the SQL<->enum tie.
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('provision', 'reprovision', 'teardown', 'build', 'install',
                    'boot', 'force_crash', 'power', 'capture_vmcore', 'image_build',
                    'diagnostics_worker_check', 'build_install_boot'));
```

- [ ] **Step 5: Run the enum-tie + migration tests to verify they pass**

Run: `uv run python -m pytest <enum-tie test path> -v` and `uv run python -m pytest -k migrate -q`
Expected: PASS (the migration applies and the CHECK set equals the enum).

- [ ] **Step 6: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/domain/operations/jobs.py src/kdive/db/schema/0051_build_install_boot_job_kind.sql
git commit -m "feat(jobs): add build_install_boot JobKind + migration 0051 (#866)"
```

---

## Task 2: Composite worker handler + payload

**Files:**
- Modify: `src/kdive/jobs/payloads.py` (add `BuildInstallBootPayload`, register in the load map)
- Create: `src/kdive/jobs/handlers/runs/composite.py`
- Modify: `src/kdive/jobs/handlers/runs/registrar.py` (register the new handler)
- Test: `tests/jobs/handlers/runs/test_composite.py`

**Interfaces:**
- Consumes: `JobKind.BUILD_INSTALL_BOOT` (Task 1); `build_handler` / `install_handler` / `boot_handler` (`src/kdive/jobs/handlers/runs/{build,install,boot}.py`); `BuildPayload` / `RunPayload` and `load_payload` (`src/kdive/jobs/payloads.py`); `Job` (`src/kdive/domain/operations/jobs.py`).
- Produces: `composite_handler(conn, job, *, ports: RunHandlerPorts) -> str | None`; `BuildInstallBootPayload(run_id, cmdline, build_host_id)`. The handler runs build→install→boot; on the first phase that raises, it lets the error propagate (the worker marks the job failed) after the phase's own `run_steps` row records the failure; `data.failed_phase` is set on the job's failure result.

- [ ] **Step 1: Write the failing test (phase ordering + short-circuit)**

`tests/jobs/handlers/runs/test_composite.py` — drive the handler with the three executors monkeypatched to records-of-calls, so the test asserts orchestration, not build internals:

```python
import pytest
from kdive.jobs.handlers.runs import composite
from kdive.jobs.payloads import BuildInstallBootPayload

class _Recorder:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on
    def make(self, phase):
        async def _h(conn, job, **kwargs):
            self.calls.append(phase)
            if phase == self.fail_on:
                raise RuntimeError(f"{phase} boom")
            return None
        return _h

@pytest.mark.asyncio
async def test_runs_three_phases_in_order(monkeypatch, fake_conn, make_job):
    rec = _Recorder()
    monkeypatch.setattr(composite, "build_handler", rec.make("build"))
    monkeypatch.setattr(composite, "install_handler", rec.make("install"))
    monkeypatch.setattr(composite, "boot_handler", rec.make("boot"))
    job = make_job(kind="build_install_boot",
                   payload={"run_id": "<uuid>", "cmdline": None, "build_host_id": "<uuid>"})
    await composite.composite_handler(fake_conn, job, ports=fake_ports)
    assert rec.calls == ["build", "install", "boot"]

@pytest.mark.asyncio
async def test_short_circuits_on_install_failure(monkeypatch, fake_conn, make_job):
    rec = _Recorder(fail_on="install")
    monkeypatch.setattr(composite, "build_handler", rec.make("build"))
    monkeypatch.setattr(composite, "install_handler", rec.make("install"))
    monkeypatch.setattr(composite, "boot_handler", rec.make("boot"))
    job = make_job(kind="build_install_boot",
                   payload={"run_id": "<uuid>", "cmdline": None, "build_host_id": "<uuid>"})
    with pytest.raises(composite.CompositePhaseError) as ei:
        await composite.composite_handler(fake_conn, job, ports=fake_ports)
    assert ei.value.failed_phase == "install"
    assert rec.calls == ["build", "install"]   # boot never runs
```

(Use the repo's existing job/handler test fixtures — find them with `rg -n "make_job|fake_conn|RunHandlerPorts" tests/jobs`. If none fit, build minimal fakes: a `Job` with `.kind`, `.payload`, `.id`, and a connection double the executors tolerate when monkeypatched out.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run python -m pytest tests/jobs/handlers/runs/test_composite.py -v`
Expected: FAIL — `composite` module / `BuildInstallBootPayload` not defined.

- [ ] **Step 3: Add the payload**

In `src/kdive/jobs/payloads.py`, after `BuildPayload`:

```python
class BuildInstallBootPayload(RunPayload):
    """Payload for the composite build->install->boot job (ADR-0268, #866).

    Carries the build admission result (`build_host_id`, selected + leased at the
    `runs.build_install_boot` boundary) so the handler can synthesize a BuildPayload for the
    build phase; install/boot need only `run_id`.
    """

    cmdline: str | None = None
    build_host_id: str
```

Register it in the same load map / `run_id_from_payload` switch the other run-bearing kinds use (find with `rg -n "JobKind.BUILD" src/kdive/jobs/payloads.py` and mirror the `BUILD` entry, mapping `JobKind.BUILD_INSTALL_BOOT -> BuildInstallBootPayload`).

- [ ] **Step 4: Write the composite handler**

Create `src/kdive/jobs/handlers/runs/composite.py`:

```python
"""Composite build->install->boot worker handler (ADR-0268, #866).

One job runs the three phases in sequence by calling the existing per-phase executors. Each
executor commits its own `run_steps` row; the first phase that raises stops the sequence and the
error propagates to the worker (which marks the job failed), tagged with `failed_phase`.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from psycopg import AsyncConnection

from kdive.domain.operations.jobs import Job
from kdive.jobs.handlers.runs.boot import boot_handler
from kdive.jobs.handlers.runs.build import build_handler
from kdive.jobs.handlers.runs.install import install_handler
from kdive.jobs.handlers.runs.shared import RunHandlerPorts
from kdive.jobs.payloads import BuildInstallBootPayload, BuildPayload, RunPayload, dump_payload


class CompositePhaseError(RuntimeError):
    """A composite phase failed; `failed_phase` names which (build|install|boot)."""

    def __init__(self, failed_phase: str, cause: BaseException) -> None:
        super().__init__(f"{failed_phase} phase failed: {cause}")
        self.failed_phase = failed_phase
        self.__cause__ = cause


def _phase_job(job: Job, payload) -> Job:
    """A copy of `job` carrying a phase-specific payload (executors are extra='forbid')."""
    return replace(job, payload=dump_payload(payload))


async def composite_handler(conn: AsyncConnection, job: Job, *, ports: RunHandlerPorts) -> str | None:
    base = BuildInstallBootPayload.model_validate(job.payload)
    run_id = base.run_id

    build_job = _phase_job(job, BuildPayload(run_id=run_id, cmdline=base.cmdline,
                                             build_host_id=base.build_host_id))
    run_only = _phase_job(job, RunPayload(run_id=run_id))

    try:
        await build_handler(conn, build_job, resolver=ports.resolver,
                            secret_registry=ports.secret_registry,
                            transport_factories=ports.transport_factories,
                            build_phase_recorder=ports.build_phase_recorder)
    except Exception as exc:  # noqa: BLE001 - re-tagged with the failed phase, re-raised
        raise CompositePhaseError("build", exc) from exc
    try:
        await install_handler(conn, run_only, resolver=ports.resolver)
    except Exception as exc:  # noqa: BLE001
        raise CompositePhaseError("install", exc) from exc
    try:
        await boot_handler(conn, run_only, resolver=ports.resolver,
                           secret_registry=ports.secret_registry,
                           artifact_store=ports.artifact_store)
    except Exception as exc:  # noqa: BLE001
        raise CompositePhaseError("boot", exc) from exc
    return None
```

Adjust the exact `replace(...)`/`dump_payload(...)` calls to match how `Job` is constructed and how payloads are serialized in this repo (check `Job`'s definition and `dump_payload`/`load_payload` in `payloads.py`; if `Job.payload` is already a dict, drop `dump_payload`). `RunHandlerPorts` field names (`resolver`, `secret_registry`, `transport_factories`, `build_phase_recorder`, `artifact_store`) must match `src/kdive/jobs/handlers/runs/shared.py` — read it and align.

- [ ] **Step 5: Register the handler**

In `src/kdive/jobs/handlers/runs/registrar.py`, inside `register_handlers`, after the `BOOT` registration:

```python
    registry.register(
        JobKind.BUILD_INSTALL_BOOT,
        lambda conn, job: composite_handler(conn, job, ports=ports),
    )
```

Import `composite_handler` at the top.

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run python -m pytest tests/jobs/handlers/runs/test_composite.py -v`
Expected: PASS.

- [ ] **Step 7: Map `CompositePhaseError.failed_phase` onto the job failure result**

Find how a handler's failure result becomes the job's `error`/`data` (search `rg -n "failed_phase|error_category|CategorizedError|def fail" src/kdive/jobs/worker.py src/kdive/domain/errors.py`). If the worker reads structured fields off the raised error, ensure `CompositePhaseError` carries `failed_phase` into the persisted job result (the spec's failure contract: `data.failed_phase`). Add a focused test asserting the failed job's `data["failed_phase"] == "install"`.

- [ ] **Step 8: Guardrails + commit**

```bash
just lint && just type && uv run python -m pytest tests/jobs/handlers/runs/test_composite.py -q
git add src/kdive/jobs/payloads.py src/kdive/jobs/handlers/runs/composite.py src/kdive/jobs/handlers/runs/registrar.py tests/jobs/handlers/runs/test_composite.py
git commit -m "feat(jobs): composite build_install_boot handler over per-phase executors (#866)"
```

---

## Task 3: `runs.build_install_boot` MCP tool (admission + enqueue)

**Files:**
- Create: `src/kdive/mcp/tools/lifecycle/runs/composite.py`
- Modify: `src/kdive/mcp/tools/lifecycle/runs/registrar.py` (register it)
- Test: `tests/mcp/tools/lifecycle/runs/test_composite_tool.py`

**Interfaces:**
- Consumes: the build-host selection + lease + atomic enqueue path used by `runs.build` (`src/kdive/mcp/tools/lifecycle/runs/server_build.py` `_enqueue_build` and `src/kdive/services/runs/build_host_selection.py`); `with_runtime_for_run_target_kind`; `Role.OPERATOR`; `JobKind.BUILD_INSTALL_BOOT`; `BuildInstallBootPayload` (Task 2).
- Produces: the `runs.build_install_boot` tool returning the standard job-handle envelope (same shape `runs.build` returns).

- [ ] **Step 1: Read the model to copy**

Read `src/kdive/mcp/tools/lifecycle/runs/server_build.py` end to end. The composite tool replicates `build_run`'s admission (Run state check, build-host selection, lease acquisition, atomic enqueue) but enqueues `JobKind.BUILD_INSTALL_BOOT` with a `BuildInstallBootPayload` instead of `JobKind.BUILD` with a `BuildPayload`. Reuse `build_host_selection` and the atomic lease+enqueue pattern verbatim — only the kind and payload differ.

- [ ] **Step 2: Write the failing test**

`tests/mcp/tools/lifecycle/runs/test_composite_tool.py` (mirror the existing `runs.build` tool test — find it with `rg -l "runs.build" tests/mcp`):

```python
@pytest.mark.asyncio
async def test_enqueues_one_build_install_boot_job(operator_ctx, seeded_bound_run, pool):
    resp = await call_tool("runs.build_install_boot", {"run_id": str(seeded_bound_run.id)})
    assert resp.structured_content["status"] == "accepted"   # match runs.build envelope
    jobs = await fetch_jobs(pool, run_id=seeded_bound_run.id)
    assert [j.kind for j in jobs] == ["build_install_boot"]
    assert jobs[0].payload["build_host_id"]                  # admission ran

@pytest.mark.asyncio
async def test_requires_operator(viewer_ctx, seeded_bound_run):
    resp = await call_tool("runs.build_install_boot", {"run_id": str(seeded_bound_run.id)})
    assert resp.structured_content["error"]["category"] == "authorization_denied"
```

Match the exact envelope keys/fixtures the existing `runs.build` test uses.

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run python -m pytest tests/mcp/tools/lifecycle/runs/test_composite_tool.py -v`
Expected: FAIL — tool `runs.build_install_boot` not registered.

- [ ] **Step 4: Implement the tool**

Create `src/kdive/mcp/tools/lifecycle/runs/composite.py` with a handler that mirrors `server_build.build_run` but builds `BuildInstallBootPayload` and enqueues `JobKind.BUILD_INSTALL_BOOT`. Register it in `registrar.py` next to `runs.build`:

```python
@app.tool(name="runs.build_install_boot", annotations=_docmeta.mutating(),
          meta={"maturity": "implemented"})
async def runs_build_install_boot(
    run_id: Annotated[str, Field(description="A created, bound, not-yet-built Run to drive "
                                 "build->install->boot as one job.")],
    cmdline: Annotated[str | None, Field(description="Kernel debug args (as runs.build).")] = None,
    idempotency_key: Annotated[str | None, Field(description="Replay-safe key.")] = None,
) -> ToolResponse:
    """Build, install, and boot a bound Run as a single pollable job (#866)."""
    ctx = current_context()
    return await with_runtime_for_run_target_kind(
        pool, resolver, ctx, run_id,
        lambda runtime: _composite_handlers(runtime).build_install_boot(
            pool, ctx, run_id, cmdline=cmdline, idempotency_key=idempotency_key),
        required_role=Role.OPERATOR,
    )
```

(`required_role=Role.OPERATOR` per ADR; `runs.build` uses `CONTRIBUTOR`, the composite raises the bar to the max of its phases — `boot`/`install` are operator-level. Verify against `_TOOL_SCOPES`/handler roles for install/boot and set the max.)

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run python -m pytest tests/mcp/tools/lifecycle/runs/test_composite_tool.py -v`
Expected: PASS.

- [ ] **Step 6: Guardrails + commit**

```bash
just lint && just type && uv run python -m pytest tests/mcp/tools/lifecycle/runs/test_composite_tool.py -q
git add src/kdive/mcp/tools/lifecycle/runs/composite.py src/kdive/mcp/tools/lifecycle/runs/registrar.py tests/mcp/tools/lifecycle/runs/test_composite_tool.py
git commit -m "feat(mcp): runs.build_install_boot composite tool (#866)"
```

---

## Task 4: `tools.invoke` dispatcher

**Files:**
- Create: `src/kdive/mcp/tools/gateway.py`
- Modify: `src/kdive/mcp/tool_registration.py` (register the gateway plane)
- Test: `tests/mcp/tools/test_gateway_invoke.py`

**Interfaces:**
- Consumes: `app.call_tool` (FastMCP 3.4.2, `run_middleware=True`); `NotFoundError` (`from fastmcp.exceptions import NotFoundError` — confirm the import path with `rg -n "NotFoundError" .venv/.../fastmcp`); `ValidationError` (pydantic); `_docmeta.destructive()`; envelope helpers in `src/kdive/mcp/responses.py` (find the `configuration_error` constructor with `rg -n "configuration_error|def .*error" src/kdive/mcp/responses.py`).
- Produces: `tools.invoke(name, arguments)` returning the inner tool's `ToolResult`, or a `configuration_error` envelope for unknown name / bad arguments.

- [ ] **Step 1: Write the failing tests**

`tests/mcp/tools/test_gateway_invoke.py`:

```python
@pytest.mark.asyncio
async def test_invoke_dispatches_to_inner_tool(viewer_ctx):
    resp = await call_tool("tools.invoke", {"name": "session.whoami", "arguments": {}})
    assert resp.structured_content["data"]["principal"]      # whoami ran

@pytest.mark.asyncio
async def test_unknown_inner_name_is_configuration_error(viewer_ctx):
    resp = await call_tool("tools.invoke", {"name": "no.such_tool", "arguments": {}})
    assert resp.structured_content["error"]["category"] == "configuration_error"
    assert "tools.search" in resp.structured_content["error"]["message"]

@pytest.mark.asyncio
async def test_bad_arguments_is_configuration_error(viewer_ctx):
    # runs.get requires run_id; omit it
    resp = await call_tool("tools.invoke", {"name": "runs.get", "arguments": {}})
    assert resp.structured_content["error"]["category"] == "configuration_error"

@pytest.mark.asyncio
async def test_inner_authorization_denial_propagates(viewer_ctx, seeded_bound_run):
    # control.force_crash is admin-only; viewer must be denied identically to a direct call
    resp = await call_tool("tools.invoke",
                           {"name": "control.force_crash", "arguments": {"run_id": "..."}})
    assert resp.structured_content["error"]["category"] == "authorization_denied"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/mcp/tools/test_gateway_invoke.py -v`
Expected: FAIL — `tools.invoke` not registered.

- [ ] **Step 3: Implement the dispatcher**

Create `src/kdive/mcp/tools/gateway.py`:

```python
"""The tool gateway: tools.invoke (dispatcher) + tools.search (discovery) (ADR-0268, #866)."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError   # verify exact path
from fastmcp.tools import ToolResult
from pydantic import Field, ValidationError

from kdive.mcp.tools import _docmeta
from kdive.mcp.responses import configuration_error   # verify exact name


def register(app: FastMCP) -> None:
    @app.tool(name="tools.invoke", annotations=_docmeta.destructive(),
              meta={"maturity": "implemented"})
    async def tools_invoke(
        name: Annotated[str, Field(description="The tool to call (from tools.search).")],
        arguments: Annotated[dict[str, Any] | None,
                             Field(description="Arguments object for that tool.")] = None,
    ) -> ToolResult:
        """Call any registered tool by name (gateway dispatch, ADR-0268)."""
        try:
            return await app.call_tool(name, arguments or {}, run_middleware=True)
        except NotFoundError:
            return configuration_error(
                reason="unknown_tool",
                message=f"No tool named {name!r}; discover tools with tools.search.")
        except ValidationError as exc:
            return configuration_error(
                reason="invalid_arguments",
                message=f"Arguments for {name!r} failed validation: {exc.errors()}")
```

Adjust `configuration_error(...)`'s signature to the repo's actual helper (it may return a `ToolResponse`; ensure the return type is compatible with what FastMCP expects from a tool — check how other tools build error envelopes). Do **not** add `tools.invoke` to `_docmeta.DESTRUCTIVE_TOOLS`.

- [ ] **Step 4: Register the gateway plane**

In `src/kdive/mcp/tool_registration.py`, import `from kdive.mcp.tools import gateway` and add `_pool_only_plane_registrar`-style entry. `gateway.register` needs only `app` (no pool); add a thin adapter matching the registrar signature (look at how `session.register` — also pool-only — is wrapped, and mirror it; if `gateway.register(app)` takes no pool, wrap with a lambda dropping the extra args).

- [ ] **Step 5: Run to verify pass**

Run: `uv run python -m pytest tests/mcp/tools/test_gateway_invoke.py -v`
Expected: PASS.

- [ ] **Step 6: Guardrails + commit**

```bash
just lint && just type && uv run python -m pytest tests/mcp/tools/test_gateway_invoke.py -q
git add src/kdive/mcp/tools/gateway.py src/kdive/mcp/tool_registration.py tests/mcp/tools/test_gateway_invoke.py
git commit -m "feat(mcp): tools.invoke gateway dispatcher (#866)"
```

---

## Task 5: `tools.search` discovery + `tool_index`

**Files:**
- Create: `src/kdive/mcp/tool_index.py` (curated keywords map; namespace TOC comes in Task 8)
- Modify: `src/kdive/mcp/tools/gateway.py` (add `tools.search`)
- Test: `tests/mcp/tools/test_gateway_search.py`, `tests/mcp/test_tool_index.py`

**Interfaces:**
- Consumes: `visible_tool_names` / `tool_visible` (`src/kdive/mcp/exposure.py`) for RBAC filtering; the registry + per-tool schema serialization that feeds `list_tools` (find with `rg -n "registered_tools|inputSchema|def .*schema" src/kdive/mcp/schema_advertising.py`); `request_context()`.
- Produces: `tools.search(query?, namespace?, limit?)` returning `{matches: [{name, description, input_schema}], truncated: bool}`; `TOOL_KEYWORDS: dict[str, frozenset[str]]`.

- [ ] **Step 1: Write the failing tests**

`tests/mcp/tools/test_gateway_search.py`:

```python
@pytest.mark.asyncio
async def test_query_ranks_relevant_tool_first(operator_ctx):
    resp = await call_tool("tools.search", {"query": "boot a built kernel"})
    names = [m["name"] for m in resp.structured_content["data"]["matches"]]
    assert "runs.boot" in names

@pytest.mark.asyncio
async def test_namespace_browse_returns_plane(operator_ctx):
    resp = await call_tool("tools.search", {"namespace": "debug", "limit": 50})
    names = {m["name"] for m in resp.structured_content["data"]["matches"]}
    assert {"debug.read_memory", "debug.set_breakpoint"} <= names

@pytest.mark.asyncio
async def test_payload_is_capped(operator_ctx):
    resp = await call_tool("tools.search", {"namespace": "debug", "limit": 3})
    assert len(resp.structured_content["data"]["matches"]) == 3
    assert resp.structured_content["data"]["truncated"] is True

@pytest.mark.asyncio
async def test_results_rbac_filtered(viewer_ctx):
    resp = await call_tool("tools.search", {"query": "force crash"})
    names = {m["name"] for m in resp.structured_content["data"]["matches"]}
    assert "control.force_crash" not in names   # admin-only, hidden from a viewer

@pytest.mark.asyncio
async def test_match_includes_full_input_schema(operator_ctx):
    resp = await call_tool("tools.search", {"query": "get a run"})
    match = next(m for m in resp.structured_content["data"]["matches"] if m["name"] == "runs.get")
    assert "run_id" in str(match["input_schema"])
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/mcp/tools/test_gateway_search.py -v`
Expected: FAIL — `tools.search` not registered.

- [ ] **Step 3: Add the keyword map**

Create `src/kdive/mcp/tool_index.py` with `TOOL_KEYWORDS: dict[str, frozenset[str]]` — curated terms for tools whose name/description alone rank poorly (start with the reproduce + debug planes; default to tokenised name+description when absent). Add a completeness-style test in `tests/mcp/test_tool_index.py` asserting every key is a live tool name (mirror the `CLASSIFIED_TOOLS` guard idiom).

- [ ] **Step 4: Implement `tools.search`**

Add to `gateway.register`. Pseudocode for the body (fill with real registry access):

```python
@app.tool(name="tools.search", annotations=_docmeta.read_only(), meta={"maturity": "implemented"})
async def tools_search(
    query: Annotated[str | None, Field(description="Capability to search for.")] = None,
    namespace: Annotated[str | None, Field(description="Browse one plane, e.g. 'debug'.")] = None,
    limit: Annotated[int, Field(ge=1, le=_SEARCH_LIMIT_MAX, description="Max matches.")] = 10,
) -> ToolResponse:
    """Find tools by capability or namespace; returns full schemas to build a tools.invoke call."""
    ctx = current_context()
    candidates = _rbac_visible_tools(app, ctx)              # reuse exposure.tool_visible
    ranked = _rank(candidates, query=query, namespace=namespace)  # deterministic lexical
    matches = ranked[:limit]
    if not matches and query:
        _log.info("tool_search_miss", extra={"query": query})
    return success(data={"matches": [_describe(t) for t in matches],
                         "truncated": len(ranked) > limit})
```

`_SEARCH_LIMIT_MAX` is the hard cap (e.g. 25). `_describe` serializes name + description + the same input schema `list_tools` emits. Ranking is deterministic lexical over name + description + `TOOL_KEYWORDS`.

- [ ] **Step 5: Run to verify pass**

Run: `uv run python -m pytest tests/mcp/tools/test_gateway_search.py tests/mcp/test_tool_index.py -v`
Expected: PASS.

- [ ] **Step 6: Guardrails + commit**

```bash
just lint && just type && uv run python -m pytest tests/mcp/tools/test_gateway_search.py tests/mcp/test_tool_index.py -q
git add src/kdive/mcp/tool_index.py src/kdive/mcp/tools/gateway.py tests/mcp/tools/test_gateway_search.py tests/mcp/test_tool_index.py
git commit -m "feat(mcp): tools.search discovery + curated keyword index (#866)"
```

---

## Task 6: Meta-tool skip-set across recording/audit middleware

**Files:**
- Modify: `src/kdive/mcp/middleware/shared.py` (add `META_TOOLS`)
- Modify: `src/kdive/mcp/middleware/usage.py`, `.../telemetry.py`, `.../denial_audit.py`
- Test: `tests/mcp/middleware/test_gateway_skip.py` (plus extend existing usage/denial tests)

**Interfaces:**
- Consumes: `context.message.name` (the called tool name, already used in `usage.py:_record`).
- Produces: `META_TOOLS: frozenset[str] = frozenset({"tools.invoke", "tools.search"})`; each middleware records nothing when the call's name is in `META_TOOLS`.

- [ ] **Step 1: Write the failing test**

`tests/mcp/middleware/test_gateway_skip.py` — drive a gateway call end to end and assert single-row recording:

```python
@pytest.mark.asyncio
async def test_invoke_writes_one_usage_row_keyed_to_inner(operator_ctx, pool):
    await call_tool("tools.invoke", {"name": "session.whoami", "arguments": {}})
    rows = await fetch_usage_rows(pool)
    assert [r.tool for r in rows] == ["session.whoami"]   # not tools.invoke

@pytest.mark.asyncio
async def test_denied_invoke_writes_one_denial_row_keyed_to_inner(viewer_ctx, pool, seeded_bound_run):
    await call_tool("tools.invoke",
                    {"name": "control.force_crash", "arguments": {"run_id": "..."}})
    denials = await fetch_audit_denials(pool)
    assert [d.tool for d in denials] == ["control.force_crash"]   # exactly one, inner-keyed
```

(Use the existing usage/denial test helpers — `tests/mcp/middleware/test_usage.py`, `test_denial_audit.py`.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/mcp/middleware/test_gateway_skip.py -v`
Expected: FAIL — two rows (outer `tools.invoke` + inner), and/or two denial rows.

- [ ] **Step 3: Add `META_TOOLS` and the skips**

In `src/kdive/mcp/middleware/shared.py`:

```python
META_TOOLS: frozenset[str] = frozenset({"tools.invoke", "tools.search"})
```

In `usage.py` `_record` (and the `on_call_tool` denial/exception branches), at the top:

```python
        tool = getattr(context.message, "name", "?")
        if tool in META_TOOLS:
            return
```

In `denial_audit.py`, before it writes a denial row, return early when `context.message.name in META_TOOLS`. In `telemetry.py`, skip the span/metric for `META_TOOLS` names (or tag them so they are not double-counted — match the file's structure). Read each middleware first; place the guard at the single recording site in each.

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/mcp/middleware/test_gateway_skip.py tests/mcp/middleware/test_usage.py tests/mcp/middleware/test_denial_audit.py -v`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type && uv run python -m pytest tests/mcp/middleware -q
git add src/kdive/mcp/middleware/shared.py src/kdive/mcp/middleware/usage.py src/kdive/mcp/middleware/telemetry.py src/kdive/mcp/middleware/denial_audit.py tests/mcp/middleware/test_gateway_skip.py
git commit -m "feat(mcp): skip meta-tools in usage/telemetry/denial-audit recording (#866)"
```

---

## Task 7: Core-set tier filter + `KDIVE_MCP_TOOL_GATEWAY` (default off)

**Files:**
- Modify: `src/kdive/mcp/exposure.py` (add `CORE_TOOLS`)
- Modify: `src/kdive/mcp/middleware/exposure.py` (chain the filter behind the flag)
- Test: `tests/mcp/middleware/test_exposure.py` (extend)

**Interfaces:**
- Consumes: `visible_tool_names` (existing RBAC filter); `os.environ` for the flag (mirror an existing `KDIVE_*` read — find one with `rg -n "os.environ.get\(\"KDIVE_" src/kdive`).
- Produces: `CORE_TOOLS: frozenset[str]`; `on_list_tools` returns `rbac_visible ∩ CORE_TOOLS` only when the flag is on.

- [ ] **Step 1: Write the failing tests**

In `tests/mcp/middleware/test_exposure.py`:

```python
@pytest.mark.asyncio
async def test_gateway_off_returns_full_rbac_catalog(operator_ctx, monkeypatch):
    monkeypatch.delenv("KDIVE_MCP_TOOL_GATEWAY", raising=False)
    tools = await list_tools(operator_ctx)
    assert len(tools) > 20            # full RBAC-scoped catalog, not the core set

@pytest.mark.asyncio
async def test_gateway_on_returns_core_intersect_rbac(operator_ctx, monkeypatch):
    monkeypatch.setenv("KDIVE_MCP_TOOL_GATEWAY", "on")
    names = {t.name for t in await list_tools(operator_ctx)}
    assert names <= CORE_TOOLS
    assert {"tools.search", "tools.invoke", "runs.build_install_boot"} <= names

@pytest.mark.asyncio
async def test_gateway_on_fails_open_on_error(operator_ctx, monkeypatch):
    monkeypatch.setenv("KDIVE_MCP_TOOL_GATEWAY", "on")
    # force the intersection to raise; assert the full catalog returns, not empty
    ...
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/mcp/middleware/test_exposure.py -v`
Expected: FAIL — `CORE_TOOLS` undefined / no flag handling.

- [ ] **Step 3: Add `CORE_TOOLS`**

In `src/kdive/mcp/exposure.py`:

```python
#: The default-listed core set when the gateway is on (ADR-0268). Tunable from tool_invocation
#: data. Everything else is reachable via tools.search + tools.invoke. CORE_TOOLS must be a subset
#: of the live registry (guard test in tests/mcp/core/test_app.py).
CORE_TOOLS: frozenset[str] = frozenset({
    "tools.search", "tools.invoke", "session.whoami",
    "runs.build_install_boot", "runs.create", "runs.get", "runs.list",
    "allocations.request", "allocations.wait", "systems.provision",
})
```

- [ ] **Step 4: Chain the flag into `on_list_tools`**

In `src/kdive/mcp/middleware/exposure.py`, after computing `visible` and before returning, intersect with `CORE_TOOLS` when the flag is on, inside the existing try/except so any error still falls through to the full-catalog return:

```python
        visible = visible_tool_names(ctx, (tool.name for tool in tools))
        if _gateway_enabled():
            visible &= CORE_TOOLS
    except AuthError:
        ...
```

```python
def _gateway_enabled() -> bool:
    return os.environ.get("KDIVE_MCP_TOOL_GATEWAY", "off").strip().lower() in {"on", "1", "true"}
```

Keep the broad `except Exception:` fail-open branch intact.

- [ ] **Step 5: Run to verify pass**

Run: `uv run python -m pytest tests/mcp/middleware/test_exposure.py -v`
Expected: PASS.

- [ ] **Step 6: Guardrails + commit**

```bash
just lint && just type && uv run python -m pytest tests/mcp/middleware/test_exposure.py -q
git add src/kdive/mcp/exposure.py src/kdive/mcp/middleware/exposure.py tests/mcp/middleware/test_exposure.py
git commit -m "feat(mcp): core-set tier filter behind KDIVE_MCP_TOOL_GATEWAY (default off) (#866)"
```

---

## Task 8: Server `instructions` table of contents

**Files:**
- Modify: `src/kdive/mcp/tool_index.py` (add `NAMESPACE_TOC` + `build_instructions()`)
- Modify: `src/kdive/mcp/app.py` (pass `instructions=` to `FastMCP(...)`)
- Test: `tests/mcp/test_tool_index.py` (extend)

**Interfaces:**
- Consumes: the set of live namespaces (derive from registered tool names: the prefix before the first `.`).
- Produces: `build_instructions() -> str` carrying the gateway pattern + a namespace TOC.

- [ ] **Step 1: Write the failing test**

```python
def test_instructions_cover_every_live_namespace(built_app):
    text = built_app.instructions
    live_ns = {name.split(".")[0] for name in registered_tool_names(built_app)}
    for ns in live_ns:
        assert ns in text
    assert "tools.search" in text and "tools.invoke" in text
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/mcp/test_tool_index.py -k instructions -v`
Expected: FAIL — no `instructions` on the app.

- [ ] **Step 3: Implement**

In `tool_index.py` add `NAMESPACE_TOC: dict[str, str]` (one-liner per namespace) and `build_instructions()` returning the gateway-pattern paragraph + the TOC. In `app.py`:

```python
    app: FastMCP = FastMCP(name="kdive", auth=verifier or build_verifier(),
                           instructions=build_instructions())
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/mcp/test_tool_index.py -k instructions -v`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type && uv run python -m pytest tests/mcp/test_tool_index.py -q
git add src/kdive/mcp/tool_index.py src/kdive/mcp/app.py tests/mcp/test_tool_index.py
git commit -m "feat(mcp): server instructions with gateway pattern + namespace TOC (#866)"
```

---

## Task 9: Classify the three new tools (completeness guard)

**Files:**
- Modify: `src/kdive/mcp/exposure.py` (`PUBLIC_TOOLS` / `_TOOL_SCOPES`)
- Test: `tests/mcp/core/test_app.py` (the existing completeness guard must pass)

**Interfaces:**
- Consumes: the completeness guard asserting `CLASSIFIED_TOOLS | PUBLIC_TOOLS == live registry` and `CORE_TOOLS ⊆ live registry`.

- [ ] **Step 1: Run the completeness guard to see it fail**

Run: `uv run python -m pytest tests/mcp/core/test_app.py -v`
Expected: FAIL — `tools.invoke`, `tools.search`, `runs.build_install_boot` are unclassified.

- [ ] **Step 2: Classify**

In `exposure.py`: add `"tools.invoke"` and `"tools.search"` to `PUBLIC_TOOLS`; add `"runs.build_install_boot": _OPERATOR` to `_TOOL_SCOPES`.

- [ ] **Step 3: Run the guard + the CORE_TOOLS subset guard**

Run: `uv run python -m pytest tests/mcp/core/test_app.py -v`
Expected: PASS. If `tests/mcp/core/test_app.py` lacks a `CORE_TOOLS ⊆ registry` assertion, add one.

- [ ] **Step 4: Guardrails + commit**

```bash
just lint && just type && uv run python -m pytest tests/mcp/core/test_app.py -q
git add src/kdive/mcp/exposure.py tests/mcp/core/test_app.py
git commit -m "feat(mcp): classify gateway tools + composite for exposure guard (#866)"
```

---

## Task 10: Full-suite gate + RBAC doc regeneration

**Files:**
- Possibly: a generated RBAC tool matrix (the reverted PR touched one — find with `rg -ln "RBAC tool matrix|tool matrix" docs`).

- [ ] **Step 1: Regenerate any committed tool/RBAC matrix**

The new tools change the generated RBAC matrix. Find the generator (`rg -n "matrix" justfile docs`), regenerate, and review the diff.

- [ ] **Step 2: Run the full suite**

Run: `just lint && just type && just test`
Expected: all green (skips for Docker/live are acceptable per AGENTS.md).

- [ ] **Step 3: Commit any regenerated artifacts**

```bash
git add -A
git commit -m "docs(mcp): regenerate RBAC tool matrix for gateway tools (#866)"
```

---

## Self-Review

**Spec coverage:** `tools.invoke` (T4), `tools.search` + bounded payload + namespace browse + search-miss (T5), composite as one worker job over per-phase executors (T2) + admission tool (T3) + migration (T1), skip-set incl. denial-audit (T6), core filter default-off + fail-open (T7), instructions TOC (T8), classification/completeness (T9), full-suite + generated docs (T10). The `ValidationError`/`NotFoundError` envelope-equivalence and the inner-`AuthorizationError`-propagation are covered by T4 tests; single-row denial attribution by T6.

**Deferred to the follow-up PR (per spec Verification):** flipping `KDIVE_MCP_TOOL_GATEWAY` to default-on and the cold-start end-to-end run against the real Claude Code client. This PR ships the machinery off by default.

**Placeholder scan:** no `TBD`/`handle edge cases`; each task has real code or an explicit "read X and mirror it" with the exact path. Where a repo-specific signature must be confirmed (envelope helper name, `RunHandlerPorts` fields, `NotFoundError` import, `Job` construction), the step names the file to read — this is required because those signatures are not safely guessable, not a placeholder.

**Type consistency:** `BuildInstallBootPayload(run_id, cmdline, build_host_id)` defined in T2 and produced by T3; `CompositePhaseError.failed_phase` defined and consumed in T2; `CORE_TOOLS` defined T7, classified-guarded T9; `META_TOOLS` defined T6 and referenced in three middlewares.
