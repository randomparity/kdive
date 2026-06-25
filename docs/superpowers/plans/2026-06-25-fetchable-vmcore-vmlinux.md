# Fetchable raw vmcore + vmlinux (egress) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an MCP tool `artifacts.fetch_raw(run_id, asset)` that mints a presigned download URL for a Run's raw vmcore or vmlinux, gated by project membership + `contributor`, so the owning project's agent can run drgn locally.

**Architecture:** A new handler module `mcp/tools/catalog/artifacts/raw_fetch.py` resolves the asset object key from existing row data (`vmlinux` ← `runs.debuginfo_ref`; `vmcore` ← `raw_vmcore_key(run.system_id)`), authorizes against the asset's owning project, HEADs the object for existence/size, presigns a download URL, and audits the egress. Wired through the existing `artifacts` registrar. No schema migration, no write-path change.

**Tech Stack:** Python 3.14, FastMCP, psycopg (async), the kdive object-store seam (`object_store_from_env`), `ToolResponse` envelope, project-scoped RBAC (`require_role`).

## Global Constraints

- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` strict, whole-tree (`just type` covers `src` + `tests`).
- Absolute imports only (no relative `..`).
- Every tool returns a `ToolResponse`; a failure status carries an `error_category`; pick the most specific existing `ErrorCategory`/`ConfigErrorReason` — never invent strings.
- `asset` is a **closed** set `{"vmcore", "vmlinux"}` — the egress allow-list. No other artifact kind is resolvable.
- Cross-project isolation: a non-member sees a `not_found`-shaped envelope (no existence leak). Role gate is `contributor`, checked against **each asset's own owning project** (`run.project` for vmlinux, `system.project` for vmcore).
- URL-only: never return inline bytes (multi-GB binaries). Reuse `KDIVE_ARTIFACT_DOWNLOAD_TTL_SECONDS` (`ARTIFACT_DOWNLOAD_TTL_SECONDS`); no new env var.
- Audit every successful egress via `kdive.security.audit.record`.
- Guardrails before each commit: `just lint`, `just type`, the focused tests; before the final push also `just docs-check`, `just adr-status-check`, `just docs-links`, `just docs-paths`, `just resources-docs-check`, and the full `just test`.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

## File structure

- Create: `src/kdive/mcp/tools/catalog/artifacts/raw_fetch.py` — the `fetch_raw` handler + `RawAsset` enum + object-key resolution + authz + presign + audit.
- Modify: `src/kdive/mcp/tools/catalog/artifacts/registrar.py` — register `artifacts.fetch_raw`.
- Modify: `src/kdive/db/artifact_queries.py` — add an async `debuginfo_ref_for_run` reader returning `(project, system_id, debuginfo_ref)` (or reuse existing readers; see Task 1).
- Create: `tests/mcp/catalog/test_raw_fetch_tool.py` — behavior + edge/authz tests.
- Regenerate: `docs/guide/reference/artifacts.md` (via `just docs`).

---

### Task 1: Run/asset resolution query

**Files:**
- Modify: `src/kdive/db/artifact_queries.py`
- Test: `tests/db/test_artifact_queries.py` (create if absent; otherwise append)

**Interfaces:**
- Consumes: existing `raw_vmcore_key(conn, system_id) -> str | None` (already in this file).
- Produces: `async def run_fetch_context(conn, run_id: UUID) -> RunFetchContext | None` returning a frozen dataclass/NamedTuple `RunFetchContext(project: str, system_id: UUID | None, debuginfo_ref: str | None)`. Returns `None` when the Run row is absent. Also `async def system_project(conn, system_id: UUID) -> str | None`.

- [ ] **Step 1: Write the failing test** — seed a Run + System, assert `run_fetch_context` returns the row's `project`, `system_id`, `debuginfo_ref`, and `None` for an absent run; `system_project` returns the System's project and `None` for an absent system.

```python
# tests/db/test_artifact_queries.py
import pytest
from kdive.db.artifact_queries import run_fetch_context, system_project

@pytest.mark.asyncio
async def test_run_fetch_context_returns_row_fields(seeded_run_conn):
    conn, run_id, system_id, project = seeded_run_conn
    ctx = await run_fetch_context(conn, run_id)
    assert ctx is not None
    assert ctx.project == project
    assert ctx.system_id == system_id
    assert await system_project(conn, system_id) == project

@pytest.mark.asyncio
async def test_run_fetch_context_absent_run_is_none(db_conn):
    from uuid import uuid4
    assert await run_fetch_context(db_conn, uuid4()) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/db/test_artifact_queries.py -q`
Expected: FAIL with `ImportError: cannot import name 'run_fetch_context'`.

- [ ] **Step 3: Write minimal implementation** — add to `src/kdive/db/artifact_queries.py`:

```python
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class RunFetchContext:
    project: str
    system_id: UUID | None
    debuginfo_ref: str | None

_RUN_FETCH_CONTEXT_SQL: LiteralString = (
    "SELECT project, system_id, debuginfo_ref FROM runs WHERE id = %s"
)
_SYSTEM_PROJECT_SQL: LiteralString = "SELECT project FROM systems WHERE id = %s"

async def run_fetch_context(conn: AsyncConnection, run_id: UUID) -> RunFetchContext | None:
    """Return the Run's project, bound System id, and vmlinux ref, or ``None`` if absent."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_RUN_FETCH_CONTEXT_SQL, (run_id,))
        row = await cur.fetchone()
    if row is None:
        return None
    ref = row["debuginfo_ref"]
    return RunFetchContext(
        project=str(row["project"]),
        system_id=row["system_id"],
        debuginfo_ref=str(ref) if isinstance(ref, str) and ref else None,
    )

async def system_project(conn: AsyncConnection, system_id: UUID) -> str | None:
    """Return a System's owning project, or ``None`` if the row is absent."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_SYSTEM_PROJECT_SQL, (system_id,))
        row = await cur.fetchone()
    return None if row is None else str(row["project"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/db/test_artifact_queries.py -q`
Expected: PASS. Then `just lint && just type`.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/db/artifact_queries.py tests/db/test_artifact_queries.py
git commit -m "feat(artifacts): add run_fetch_context + system_project readers (#781)"
```

---

### Task 2: `fetch_raw` handler — happy path (vmlinux + vmcore)

**Files:**
- Create: `src/kdive/mcp/tools/catalog/artifacts/raw_fetch.py`
- Test: `tests/mcp/catalog/test_raw_fetch_tool.py`

**Interfaces:**
- Consumes: `run_fetch_context`, `system_project` (Task 1); `raw_vmcore_key`; `object_store_from_env`; `presign_get(key, expires_in)`, `head(key) -> HeadResult | None`; `require_role(ctx, project, Role.CONTRIBUTOR)`; `audit.record`; `ARTIFACT_DOWNLOAD_TTL_SECONDS`; `_common.config_error`, `_common.not_found`, `_common.as_uuid`.
- Produces: `class RawAsset(StrEnum)` with `VMCORE = "vmcore"`, `VMLINUX = "vmlinux"`; `async def fetch_raw(pool, ctx, *, run_id: str, asset: RawAsset, store_factory=object_store_from_env) -> ToolResponse`.

**Handler contract (encode exactly):**
1. `as_uuid(run_id)`; `None` → `config_error(run_id)`.
2. `run_fetch_context(conn, uid)`; `None` or `ctx.project not in ctx.projects` → `not_found(run_id)`.
3. `vmlinux` branch: `require_role(ctx, run.project, Role.CONTRIBUTOR)`; key = `run.debuginfo_ref`; if `None` → `config_error(run_id, data={"reason": "vmlinux_unavailable"})`.
4. `vmcore` branch: if `run.system_id is None` → `config_error(run_id, data={"reason": "vmcore_unavailable"})`; else `sysproj = system_project(conn, system_id)`; if `None` or `sysproj not in ctx.projects` → `not_found(run_id)`; `require_role(ctx, sysproj, Role.CONTRIBUTOR)`; key = `raw_vmcore_key(conn, system_id)`; if `None` → `config_error(run_id, data={"reason": "vmcore_unavailable"})`.
5. `head = store.head(key)` (in `asyncio.to_thread`); `None` → `config_error(run_id, data={"reason": f"{asset.value}_unavailable"})`.
6. `url = store.presign_get(key, expires_in=ttl)` (`asyncio.to_thread`).
7. `audit.record(conn, ctx, AuditEvent(tool="artifacts.fetch_raw", object_kind="runs", object_id=uid, transition="fetch_raw", args={"run_id": run_id, "asset": asset.value}, project=run.project))`.
8. Return `ToolResponse.success(run_id, "available", suggested_next_actions=["artifacts.fetch_raw"], refs={"download_uri": url}, data={"asset": asset.value, "size_bytes": str(head.size_bytes), "ttl": str(ttl)})`.

Store-factory/`CategorizedError` failures map via `ToolResponse.failure_from_error(run_id, exc)` (mirror `reads.py`).

- [ ] **Step 1: Write the failing test** — vmlinux + vmcore happy paths with a fake store. Use `_ctx(Role.CONTRIBUTOR)` and a seeded Run/System whose `debuginfo_ref` and raw vmcore key resolve. Assert `refs["download_uri"]` is the fake's URL, `data["asset"]`, `data["size_bytes"]`, and that **no inline content** field is present.

```python
# tests/mcp/catalog/test_raw_fetch_tool.py (sketch — mirror test_artifacts_tools.py fixtures)
import pytest
from kdive.mcp.tools.catalog.artifacts.raw_fetch import RawAsset, fetch_raw
from kdive.security.authz.rbac import Role

@pytest.mark.asyncio
async def test_fetch_raw_vmlinux_presigns_url(seeded_run_with_vmlinux, fake_store):
    pool, run_id, project = seeded_run_with_vmlinux
    ctx = _ctx(Role.CONTRIBUTOR, projects=(project,))
    resp = await fetch_raw(pool, ctx, run_id=run_id, asset=RawAsset.VMLINUX,
                           store_factory=lambda: fake_store)
    assert resp.status == "available"
    assert resp.refs["download_uri"] == fake_store.url
    assert resp.data["asset"] == "vmlinux"
    assert "content" not in resp.data
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/mcp/catalog/test_raw_fetch_tool.py -q`
Expected: FAIL (`ModuleNotFoundError: ...raw_fetch`).

- [ ] **Step 3: Write minimal implementation** — create `raw_fetch.py` with `RawAsset` + `fetch_raw` per the handler contract above. Model the store seam as a `Protocol` with `head` + `presign_get` (mirror `reads._SearchStore`). Wrap blocking store calls in `asyncio.to_thread`. Bind `bind_context(principal=ctx.principal)` around the DB/store work.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/mcp/catalog/test_raw_fetch_tool.py -q`
Expected: PASS. Then `just lint && just type`.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/catalog/artifacts/raw_fetch.py tests/mcp/catalog/test_raw_fetch_tool.py
git commit -m "feat(artifacts): add fetch_raw handler for raw vmcore + vmlinux (#781)"
```

---

### Task 3: Edge + authorization tests

**Files:**
- Modify: `tests/mcp/catalog/test_raw_fetch_tool.py`

**Interfaces:**
- Consumes: `fetch_raw`, `RawAsset` (Task 2); `AuthorizationError`/`RoleDenied`.

Cover every branch of the handler contract:

- [ ] **Step 1: Write failing tests**
  - **Cross-project:** member of project B requests project A's run → `not_found` (no `download_uri`, no existence leak).
  - **Sub-contributor role:** `_ctx(Role.VIEWER)` member → `require_role` raises `RoleDenied` (assert the handler propagates it, matching `reads.py` which does not catch). Use `pytest.raises`.
  - **vmlinux unavailable:** Run with `debuginfo_ref = NULL` → `config_error` with `data["reason"] == "vmlinux_unavailable"`.
  - **vmcore unavailable (no core):** Run bound to a System with no raw vmcore → `data["reason"] == "vmcore_unavailable"`.
  - **vmcore, system_id NULL:** unbound Run + `RawAsset.VMCORE` → `data["reason"] == "vmcore_unavailable"`.
  - **store HEAD returns None:** fake store `head -> None` → `config_error` with the `*_unavailable` reason; assert `presign_get` was **not** called.
  - **malformed run_id:** `"not-a-uuid"` → `config_error`.

- [ ] **Step 2: Run to verify they fail / then pass after any small handler fixes**

Run: `uv run python -m pytest tests/mcp/catalog/test_raw_fetch_tool.py -q`
Expected: all PASS once the handler contract is fully implemented (Task 2). Fix any branch the tests catch. Then `just lint && just type`.

- [ ] **Step 3: Commit**

```bash
git add tests/mcp/catalog/test_raw_fetch_tool.py src/kdive/mcp/tools/catalog/artifacts/raw_fetch.py
git commit -m "test(artifacts): cover fetch_raw authz + unavailable edges (#781)"
```

---

### Task 4: Register `artifacts.fetch_raw` + regenerate the tool reference

**Files:**
- Modify: `src/kdive/mcp/tools/catalog/artifacts/registrar.py`
- Regenerate: `docs/guide/reference/artifacts.md`
- Test: `tests/mcp/catalog/test_raw_fetch_tool.py` (add a built-app param-schema assertion)

**Interfaces:**
- Consumes: `fetch_raw`, `RawAsset` (Task 2); the registrar's `current_context()` + `_docmeta` pattern.

- [ ] **Step 1: Write the failing test** — assert the built app exposes `artifacts.fetch_raw` with `run_id` (string) + `asset` (enum `vmcore`/`vmlinux`) params (mirror `_search_text_param_schema`).

```python
@pytest.mark.asyncio
async def test_fetch_raw_tool_registered():
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    kp = make_keypair()
    verifier = JWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience=AUDIENCE)
    app = build_app(pool, verifier=verifier, secret_registry=SecretRegistry())
    tool = await app.get_tool("artifacts.fetch_raw")
    assert tool is not None
    props = tool.parameters["properties"]
    assert set(props) == {"run_id", "asset"}
    assert set(props["asset"]["enum"]) == {"vmcore", "vmlinux"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/mcp/catalog/test_raw_fetch_tool.py::test_fetch_raw_tool_registered -q`
Expected: FAIL (`tool is None`).

- [ ] **Step 3: Register the tool** — add `_register_artifacts_fetch_raw(app, pool)` and call it from `register()`. Use `_docmeta.read_only()` annotations and a `partial` / `LIVE_DEPENDENCY` maturity meta (the asset only exists after a live build/capture). The `asset` param is typed `RawAsset` so FastMCP advertises the enum. Docstring: "Mint a presigned download URL for a Run's raw vmcore or vmlinux. Requires contributor."

```python
def _register_artifacts_fetch_raw(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="artifacts.fetch_raw",
        annotations=_docmeta.read_only(),
        meta=_docmeta.maturity_meta(
            "partial",
            reason=_docmeta.MaturityReason.LIVE_DEPENDENCY,
            detail=("Presigns a Run's raw vmcore/vmlinux; those objects only exist after a "
                    "live build/capture path runs, exercised under the gated live markers."),
            promotion=("A non-gated test presigns an asset a real run produced, or a recorded "
                       "live_stack run does."),
        ),
    )
    async def artifacts_fetch_raw(
        run_id: Annotated[str, Field(description="The Run whose raw asset to fetch.")],
        asset: Annotated[raw_fetch.RawAsset, Field(description="Which raw asset: vmcore or vmlinux.")],
    ) -> ToolResponse:
        """Mint a presigned download URL for a Run's raw vmcore or vmlinux. Requires contributor."""
        return await raw_fetch.fetch_raw(pool, current_context(), run_id=run_id, asset=asset)
```

- [ ] **Step 4: Regenerate docs + run tests**

Run: `just docs` (regenerates `docs/guide/reference/`), then
`uv run python -m pytest tests/mcp/catalog/test_raw_fetch_tool.py -q`, then `just lint && just type && just docs-check`.
Expected: tool registered, param-schema test PASS, `docs-check` clean.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/catalog/artifacts/registrar.py docs/guide/reference/ tests/mcp/catalog/test_raw_fetch_tool.py
git commit -m "feat(artifacts): register artifacts.fetch_raw + regen tool reference (#781)"
```

---

### Task 5: Full guardrail sweep

**Files:** none (verification only).

- [ ] **Step 1:** Run the full local gate the CI hard-gates individually:

```bash
just lint && just type && just docs-check && just adr-status-check \
  && just docs-links && just docs-paths && just config-docs-check \
  && just resources-docs-check && just test
```

Expected: all green, zero warnings. The `live_vm`/`live_stack` markers stay skipped (expected). If `resources-docs-check` or `docs-check` flags drift, run `just resources-docs` / `just docs` and re-commit the regenerated files.

- [ ] **Step 2:** If anything was regenerated, commit it:

```bash
git add -A && git commit -m "chore(artifacts): regenerate docs snapshots for fetch_raw (#781)"
```

---

## Self-review notes

- **Spec coverage:** Task 1 (resolution) + Task 2/3 (handler, authz on each owning project, URL-only, audit, `*_unavailable` reasons) + Task 4 (tool surface + regen) cover the spec's Design, Authorization, Output, and Test plan. Task 5 covers the guardrail/regeneration obligations.
- **Lifecycle/multiplicity** needs no code: per-Run vmlinux and per-System vmcore keys already give non-overwriting multiplicity; the handler reads the existing per-owner rows. (#796 tracks per-Run vmcore capture.)
- **No new env/config** → `config-guard` / `env-docs-check` unaffected.
- **Type consistency:** `RunFetchContext`/`run_fetch_context`/`system_project` (Task 1) ↔ `fetch_raw`/`RawAsset` (Task 2) ↔ registrar (Task 4) names match throughout.
