# Investigation naming + reporting fields Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Investigations an agent-settable free-form `description`, an editable `title`,
richer reporting in `get`, and a new `investigations.list`, per [spec](../../specs/2026-06-16-investigation-naming-reporting-fields.md) / [ADR-0135](../../adr/0135-investigation-naming-reporting-fields.md).

**Architecture:** One DB migration (`0037`, additive nullable column + `CHECK`), one model field,
and edits confined to `src/kdive/mcp/tools/catalog/investigations.py` plus its test module. Length
bounds are enforced at the write boundary (not as model `Field` constraints) so reads of
pre-existing rows never break. Two new tools append to the existing `investigations.*` registrar —
no entrypoint change.

**Tech Stack:** Python 3.13, FastMCP, psycopg (async), Pydantic v2, Postgres. Guardrails via
`just lint` / `just type` / `just test`. DB tests use disposable Postgres (Docker).

**Execution note:** the tasks are tightly coupled (same module + same test file), so they run
**sequentially in one working tree** — do not parallelize across worktrees.

---

### Task 1: Migration 0037 + model field + migrate-test list

**Files:**
- Create: `src/kdive/db/schema/0037_investigation_description.sql`
- Modify: `src/kdive/domain/models.py:294-300` (Investigation model)
- Modify: `tests/db/test_migrate.py` (expected-versions list, ends at `"0036"`)
- Test: `tests/db/test_migrate.py`

- [ ] **Step 1: Write the failing migrate test additions**

In `tests/db/test_migrate.py`, append `"0037",` to the expected-versions list (the one ending
`"0036",` then `]` near line 132), and add a column-presence test at the end of the file:

```python
def test_investigations_description_column(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    columns = _columns(pg_conn, "investigations")
    assert columns["description"] == "text"


def test_investigations_description_length_check(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    pg_conn.execute(
        "INSERT INTO investigations (title, state, principal, project) "
        "VALUES ('t', 'open', 'p', 'proj')"
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        pg_conn.execute(
            "UPDATE investigations SET description = repeat('x', 4097)"
        )
```

(Confirm `pytest` and `psycopg` are already imported in the file; they are.)

- [ ] **Step 2: Run to verify failure**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/db/test_migrate.py -q`
Expected: FAIL — `0037` not applied / `description` column absent.

- [ ] **Step 3: Write the migration**

`src/kdive/db/schema/0037_investigation_description.sql`:

```sql
-- ADR-0135: free-form, agent-settable description for Investigation reporting.
ALTER TABLE investigations
    ADD COLUMN description text
        CONSTRAINT investigations_description_len
        CHECK (description IS NULL OR char_length(description) <= 4096);
```

- [ ] **Step 4: Add the model field**

In `src/kdive/domain/models.py`, the `Investigation` class becomes (keep the docstring):

```python
class Investigation(DomainModel, _Attribution):
    """A project-scoped campaign grouping Runs toward a goal."""

    title: str
    description: str | None = None
    external_refs: list[ExternalRef] = Field(default_factory=list)
    state: InvestigationState
    last_run_at: datetime | None = None
```

(No `Field` length bounds — bounds live at the write boundary, Task 2.)

- [ ] **Step 5: Run to verify pass**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/db/test_migrate.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/db/schema/0037_investigation_description.sql src/kdive/domain/models.py tests/db/test_migrate.py
git commit -m "feat: add investigations.description column (ADR-0135)"
```

---

### Task 2: Write-boundary length validation + open accepts description

**Files:**
- Modify: `src/kdive/mcp/tools/catalog/investigations.py` (add `_validate_text`, extend `open_investigation` + its wrapper)
- Test: `tests/mcp/catalog/test_investigations_tools.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/mcp/catalog/test_investigations_tools.py` (reuse the existing `_open`, `_ctx`,
`migrated_url`, `pool` helpers in that file):

```python
def test_open_persists_description(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _open(pool, _ctx(), project="proj", title="t", description="oops in xfs")
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT description FROM investigations WHERE id = %s", (resp.object_id,)
                )
                row = await cur.fetchone()
            assert row["description"] == "oops in xfs"
    asyncio.run(scenario())


def test_open_empty_description_stores_null(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _open(pool, _ctx(), project="proj", title="t", description="")
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT description FROM investigations WHERE id = %s", (resp.object_id,)
                )
                row = await cur.fetchone()
            assert row["description"] is None
    asyncio.run(scenario())


def test_open_overlong_description_is_config_error(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _open(pool, _ctx(), project="proj", title="t", description="x" * 4097)
            assert resp.error_category == "configuration_error"
    asyncio.run(scenario())


def test_open_overlong_title_is_config_error(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _open(pool, _ctx(), project="proj", title="x" * 201)
            assert resp.error_category == "configuration_error"
    asyncio.run(scenario())
```

(Match the exact import/helper names already used in the test file — check the top of it; e.g.
`_pool` may be named differently. Use whatever the file already uses for pool/ctx/open.)

- [ ] **Step 2: Run to verify failure**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/mcp/catalog/test_investigations_tools.py -q -k "description or overlong_title"`
Expected: FAIL — `open_investigation` has no `description` param / no length bound.

- [ ] **Step 3: Add the boundary validator + extend open**

In `investigations.py`, add near the other module helpers:

```python
_TITLE_MAX = 200
_DESCRIPTION_MAX = 4096


def _validate_text(title: str | None, description: str | None) -> bool:
    """Return whether supplied title/description are within their write-boundary bounds.

    A ``None`` field is "not supplied" and is not checked here. ``title`` (when supplied) must be
    1..=200 chars; ``description`` (when supplied) must be 0..=4096 chars. Bounds live here, not on
    the model, so reading a pre-existing out-of-bound row never raises (ADR-0135).
    """
    if title is not None and not (1 <= len(title) <= _TITLE_MAX):
        return False
    if description is not None and len(description) > _DESCRIPTION_MAX:
        return False
    return True
```

Extend `open_investigation` signature with `description: str | None = None` and, after the
existing `_parse_external_refs` block, before building the `Investigation`:

```python
        if not _validate_text(title, description):
            return _config_error(project)
        normalized_description = description or None  # "" -> None on open (ADR-0135 §2)
```

Pass `description=normalized_description` into the `Investigation(...)` constructor.

Extend the `investigations_open` wrapper with the new optional param:

```python
        description: Annotated[
            str | None,
            Field(description="Optional free-form description for reporting (<=4096 chars)."),
        ] = None,
```

and thread it into the `open_investigation(...)` call.

- [ ] **Step 4: Run to verify pass**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/mcp/catalog/test_investigations_tools.py -q -k "description or overlong_title"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/catalog/investigations.py tests/mcp/catalog/test_investigations_tools.py
git commit -m "feat: investigations.open accepts a bounded description"
```

---

### Task 3: Enrich the rendered envelope + route open through it

**Files:**
- Modify: `src/kdive/mcp/tools/catalog/investigations.py` (`_envelope_for_investigation`, `open_investigation` success path)
- Modify: `tests/mcp/catalog/test_investigations_tools.py:116` (`data["external_refs"] == "0"` assertion)
- Test: `tests/mcp/catalog/test_investigations_tools.py`

- [ ] **Step 1: Update the existing assertion + add a get-reporting test**

In `test_investigations_tools.py`, the existing assertion at line ~116
`assert resp.data["external_refs"] == "0"` must change — `external_refs` becomes a list. Replace it
with `assert resp.data["external_refs"] == []` and `assert resp.data["title"] == ...` matching that
test's opened title. Add:

```python
def test_get_reports_title_and_description(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            opened = await _open(pool, _ctx(), project="proj", title="xfs oops", description="hyp")
            resp = await _get(pool, _ctx(), opened.object_id)
            assert resp.data["title"] == "xfs oops"
            assert resp.data["description"] == "hyp"
            assert resp.data["external_refs"] == []
            assert resp.data["state"] == "open"
            assert resp.data["last_run_at"] is None
    asyncio.run(scenario())
```

- [ ] **Step 2: Run to verify failure**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/mcp/catalog/test_investigations_tools.py -q -k "reports_title or get"`
Expected: FAIL — `data` lacks `title`/`description`; `external_refs` is `"0"` not `[]`.

- [ ] **Step 3: Enrich `_envelope_for_investigation` and route open through it**

Replace the `data=...` in `_envelope_for_investigation`:

```python
    data: dict[str, object] = {
        "project": inv.project,
        "title": inv.title,
        "description": inv.description,
        "external_refs": [r.model_dump() for r in inv.external_refs],
        "state": inv.state.value,
        "last_run_at": inv.last_run_at.isoformat() if inv.last_run_at else None,
    }
    return ToolResponse.success(
        str(inv.id), inv.state.value, suggested_next_actions=actions, data=data
    )
```

In `open_investigation`, replace the hand-built success envelope
(`return ToolResponse.success(str(inv.id), "open", ...)`) with
`return _envelope_for_investigation(inv)`.

- [ ] **Step 4: Run to verify pass (and the whole investigations file, to catch the link/unlink/get tests that read the envelope)**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/mcp/catalog/test_investigations_tools.py -q`
Expected: PASS. If a `suggested_next_actions` assertion for `open` fails, update it to
`["investigations.get", "investigations.close", "runs.create"]` (intended change, spec §4).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/catalog/investigations.py tests/mcp/catalog/test_investigations_tools.py
git commit -m "feat: surface investigation fields in the response envelope"
```

---

### Task 4: investigations.set

**Files:**
- Modify: `src/kdive/mcp/tools/catalog/investigations.py` (handler `set_investigation` + `_set_locked` + wrapper + `register`)
- Test: `tests/mcp/catalog/test_investigations_tools.py`

- [ ] **Step 1: Write failing tests**

```python
def test_set_updates_title_and_description(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            opened = await _open(pool, _ctx(), project="proj", title="old")
            resp = await _set(pool, _ctx(), opened.object_id, title="new", description="note")
            assert resp.data["title"] == "new"
            assert resp.data["description"] == "note"
    asyncio.run(scenario())


def test_set_clear_description_with_empty_string(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            opened = await _open(pool, _ctx(), project="proj", title="t", description="x")
            resp = await _set(pool, _ctx(), opened.object_id, description="")
            assert resp.data["description"] is None
    asyncio.run(scenario())


def test_set_omitting_description_leaves_it(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            opened = await _open(pool, _ctx(), project="proj", title="t", description="keep")
            resp = await _set(pool, _ctx(), opened.object_id, title="renamed")
            assert resp.data["description"] == "keep"
    asyncio.run(scenario())


def test_set_requires_at_least_one_field(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            opened = await _open(pool, _ctx(), project="proj", title="t")
            resp = await _set(pool, _ctx(), opened.object_id)
            assert resp.error_category == "configuration_error"
    asyncio.run(scenario())


def test_set_overlong_title_is_config_error(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            opened = await _open(pool, _ctx(), project="proj", title="t")
            resp = await _set(pool, _ctx(), opened.object_id, title="x" * 201)
            assert resp.error_category == "configuration_error"
    asyncio.run(scenario())


def test_set_on_closed_is_config_error(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            opened = await _open(pool, _ctx(), project="proj", title="t")
            await _close(pool, _ctx(), opened.object_id)
            resp = await _set(pool, _ctx(), opened.object_id, title="new")
            assert resp.error_category == "configuration_error"
            assert resp.data["current_status"] == "closed"
    asyncio.run(scenario())


def test_set_reads_preexisting_overlong_title(migrated_url: str) -> None:
    """Finding-1 regression: a title written before the bound stays readable/editable."""
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            async with _pool(migrated_url) as p2, p2.connection() as conn:
                await conn.execute(
                    "INSERT INTO investigations (id, title, state, principal, project) "
                    "VALUES (%s, %s, 'open', 'p', 'proj')",
                    (str(uuid4()), "y" * 300),
                )
                inv_id = (
                    await (await conn.execute(
                        "SELECT id FROM investigations WHERE title = %s", ("y" * 300,)
                    )).fetchone()
                )[0]
            resp = await _get(pool, _ctx(), str(inv_id))
            assert resp.status == "open"  # read did not raise on the 300-char title
    asyncio.run(scenario())
```

(Define a `_set` thin async wrapper in the test alongside `_open`/`_get`/`_close`, pointing at
`set_investigation`. Reuse the file's existing `_close` helper if present, else call the close
handler directly.)

- [ ] **Step 2: Run to verify failure**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/mcp/catalog/test_investigations_tools.py -q -k set_`
Expected: FAIL — `set_investigation` undefined.

- [ ] **Step 3: Implement set_investigation**

Add (mirrors the `_link_locked`/`close_investigation` patterns already in the file):

```python
async def _set_locked(
    conn: AsyncConnection,
    ctx: RequestContext,
    uid: UUID,
    *,
    title: str | None,
    description: str | None,
    project: str,
) -> ToolResponse:
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.INVESTIGATION, uid):
        current = await _get_mutable_investigation_locked(conn, uid)
        if isinstance(current, ToolResponse):
            return current
        updates: dict[str, object] = {}
        audit_args: dict[str, object] = {}
        if title is not None:
            updates["title"] = title
            audit_args["title"] = title
        if description is not None:
            updates["description"] = description or None  # "" -> NULL (clear)
            audit_args["description"] = "cleared" if description == "" else "set"
        set_sql = sql.SQL(", ").join(
            sql.SQL("{c} = {v}").format(c=sql.Identifier(c), v=sql.Placeholder(c)) for c in updates
        )
        await conn.execute(
            sql.SQL("UPDATE investigations SET {sets} WHERE id = %(uid)s").format(sets=set_sql),
            {**updates, "uid": uid},
        )
        await audit.record(
            conn,
            ctx,
            audit.AuditEvent(
                tool="investigations.set",
                object_kind="investigations",
                object_id=uid,
                transition="set",
                args=audit_args,
                project=project,
            ),
        )
        updated = current.model_copy(update=updates)
    return _envelope_for_investigation(updated)


async def set_investigation(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    investigation_id: str,
    *,
    title: str | None = None,
    description: str | None = None,
) -> ToolResponse:
    """Edit an Investigation's title and/or description (partial, value-based; ADR-0135)."""
    uid = _as_uuid(investigation_id)
    if uid is None:
        return _config_error(investigation_id)
    if title is None and description is None:
        return _config_error(investigation_id)
    if not _validate_text(title, description):
        return _config_error(investigation_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            inv = await _resolve_operator_investigation(conn, ctx, uid, investigation_id)
            if isinstance(inv, ToolResponse):
                return inv
            return await _set_locked(
                conn, ctx, uid, title=title, description=description, project=inv.project
            )
```

Add `import` for `sql` if not present: `from psycopg import sql` (check top of file; the module
already uses `conn.execute` with literal SQL — add the import if missing). Register in `register`:

```python
    @app.tool(
        name="investigations.set",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def investigations_set(
        investigation_id: Annotated[str, Field(description="The Investigation to edit.")],
        title: Annotated[
            str | None, Field(description="New title (1..=200 chars); omit to leave unchanged.")
        ] = None,
        description: Annotated[
            str | None,
            Field(description='New description (<=4096); "" clears it; omit to leave unchanged.'),
        ] = None,
    ) -> ToolResponse:
        return await set_investigation(
            pool, current_context(), investigation_id, title=title, description=description
        )
```

- [ ] **Step 4: Run to verify pass**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/mcp/catalog/test_investigations_tools.py -q -k set_`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/catalog/investigations.py tests/mcp/catalog/test_investigations_tools.py
git commit -m "feat: add investigations.set to edit title/description"
```

---

### Task 5: investigations.list

**Files:**
- Modify: `src/kdive/mcp/tools/catalog/investigations.py` (`list_investigations` + `_fetch_investigation_rows` + wrapper + `register`)
- Test: `tests/mcp/catalog/test_investigations_tools.py`

- [ ] **Step 1: Write failing tests**

```python
def test_list_scopes_to_viewer_projects(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            await _open(pool, _ctx(), project="proj", title="a")
            await _open(pool, _ctx(), project="proj", title="b")
            resp = await _list(pool, _ctx())
            assert resp.data["count"] == "2"
            assert {i.data["title"] for i in resp.items} == {"a", "b"}
    asyncio.run(scenario())


def test_list_state_filter(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            opened = await _open(pool, _ctx(), project="proj", title="a")
            await _open(pool, _ctx(), project="proj", title="b")
            await _close(pool, _ctx(), opened.object_id)
            resp = await _list(pool, _ctx(), state="open")
            assert {i.data["title"] for i in resp.items} == {"b"}
    asyncio.run(scenario())


def test_list_bad_state_is_config_error(migrated_url: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _list(pool, _ctx(), state="nonsense")
            assert resp.error_category == "configuration_error"
    asyncio.run(scenario())
```

(Add a `_list` test wrapper for `list_investigations`. `_ctx()` must hold `viewer` on `proj`;
confirm the default test ctx does — if it grants `operator`, that includes `viewer`.)

- [ ] **Step 2: Run to verify failure**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/mcp/catalog/test_investigations_tools.py -q -k "list_"`
Expected: FAIL — `list_investigations` undefined.

- [ ] **Step 3: Implement list_investigations**

```python
async def _fetch_investigation_rows(
    conn: AsyncConnection, projects: tuple[str, ...], state: InvestigationState | None
) -> list[Investigation]:
    query = "SELECT * FROM investigations WHERE project = ANY(%s)"
    params: list[object] = [list(projects)]
    if state is not None:
        query += " AND state = %s"
        params.append(state.value)
    query += " ORDER BY created_at DESC, id DESC"
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        rows = await cur.fetchall()
    return [Investigation.model_validate(row) for row in rows]


async def list_investigations(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    project: str | None = None,
    state: str | None = None,
) -> ToolResponse:
    """List the caller's viewer-project Investigations, newest-first (ADR-0135)."""
    resolved_state: InvestigationState | None = None
    if state is not None:
        try:
            resolved_state = InvestigationState(state)
        except ValueError:
            return _config_error("investigations")
    with bind_context(principal=ctx.principal):
        viewer_projects = tuple(projects_with_role(ctx, Role.VIEWER))
        if project is not None:
            viewer_projects = tuple(p for p in viewer_projects if p == project)
        async with pool.connection() as conn:
            rows = await _fetch_investigation_rows(conn, viewer_projects, resolved_state)
        items = [_envelope_for_investigation(inv) for inv in rows]
        return ToolResponse.collection(
            "investigations",
            "ok",
            items,
            suggested_next_actions=["investigations.get", "investigations.open"],
        )
```

Add imports: `from kdive.security.authz.rbac import Role, require_role, projects_with_role` (extend
the existing `require_role` import line). Register:

```python
    @app.tool(
        name="investigations.list",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def investigations_list(
        project: Annotated[
            str | None, Field(description="Restrict to one project you can view; omit for all.")
        ] = None,
        state: Annotated[
            str | None, Field(description="Filter by state (open/active/closed/abandoned).")
        ] = None,
    ) -> ToolResponse:
        return await list_investigations(pool, current_context(), project=project, state=state)
```

- [ ] **Step 4: Run to verify pass**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/mcp/catalog/test_investigations_tools.py -q -k "list_"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/catalog/investigations.py tests/mcp/catalog/test_investigations_tools.py
git commit -m "feat: add investigations.list for project-scoped reporting"
```

---

### Task 6: Doc guard map + generated docs + full guardrails

**Files:**
- Modify: `tests/mcp/core/test_tool_docs.py:82-86` (`_BEHAVIOR_TESTS_BY_TOOL`)
- Modify: any generated tool reference under `docs/` (regenerate)

- [ ] **Step 1: Register the two new tools in the doc-guard map**

In `tests/mcp/core/test_tool_docs.py`, add to `_BEHAVIOR_TESTS_BY_TOOL` (alphabetical with the
other `investigations.*` keys):

```python
    "investigations.list": ("tests/mcp/catalog/test_investigations_tools.py",),
    "investigations.set": ("tests/mcp/catalog/test_investigations_tools.py",),
```

- [ ] **Step 2: Run the tool-docs guard**

Run: `uv run python -m pytest tests/mcp/core/test_tool_docs.py -q`
Expected: PASS (every tool documented, mapped, maturity valid). If it reports a generated-doc
drift, find the generator (it is referenced in the test/`justfile`; e.g. a `just` recipe or a
`scripts/` generator) and regenerate, then commit the regenerated file.

- [ ] **Step 3: Full guardrails**

Run: `just lint && just type && KDIVE_REQUIRE_DOCKER=1 just test`
Then doc guards: `just check-mermaid && just docs-check && just docs-links`
Expected: all green. Fix every warning (zero-warnings policy).

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "test: register investigations.set/list in the doc guard"
```

---

## Self-review notes

- **Spec coverage:** Task 1 = description column + bounded title (spec §1); Task 2 = open +
  description + normalization (§2); Task 3 = enriched envelope + open routing (§4); Task 4 =
  investigations.set with raw-value branching + audit split (§3); Task 5 = investigations.list (§5);
  Task 6 = doc guard + generated docs (acceptance criterion on registration).
- **Finding-1 regression** is covered by `test_set_reads_preexisting_overlong_title`.
- **Empty-string normalization** is `open`-only (Task 2) vs `set` raw-value branch (Task 4) —
  matches spec §2/§3.
- **Watch:** the test file's actual helper names (`_pool`/`_ctx`/`_open`/`_get`/`_close`) — read
  the top of `test_investigations_tools.py` and reuse exactly; the snippets above assume those
  names. The existing `data["external_refs"] == "0"` assertion (line ~116) MUST be updated in
  Task 3 or the suite stays red.
