# Spec — Investigation naming + reporting fields (#448)

- **Issue:** [#448](https://github.com/randomparity/kdive/issues/448) (status:needs-design)
- **ADR:** [`0135`](../adr/0135-investigation-naming-reporting-fields.md)
- **Builds on:** [`0026`](../adr/0026-investigation-run-lifecycle.md) (Investigation lifecycle)
- **Date:** 2026-06-16

## Problem

`Investigation` already carries `title` and `external_refs` (`tracker`/`id`/`url`), so the
issue's "Bugzilla ID / JIRA issue" examples are covered. Three things are not:

1. There is no **free-form text field** — the issue's remaining concrete example.
2. `title` cannot be **changed** after `investigations.open`.
3. `investigations.get` returns only `{project, external_refs: <count>}`
   (`src/kdive/mcp/tools/catalog/investigations.py:69`), hiding `title`, the refs, and
   `last_run_at`; and there is **no `investigations.list`**, so a project's campaigns cannot be
   enumerated for reporting.

## Decision (per ADR-0135)

### 1. `description` column + bounded `title`

Migration `db/schema/0037_investigation_description.sql`:

```sql
ALTER TABLE investigations
    ADD COLUMN description text
        CONSTRAINT investigations_description_len
        CHECK (description IS NULL OR char_length(description) <= 4096);
```

`Investigation` model (`domain/models.py`):

```python
class Investigation(DomainModel, _Attribution):
    """A project-scoped campaign grouping Runs toward a goal."""

    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4096)
    external_refs: list[ExternalRef] = Field(default_factory=list)
    state: InvestigationState
    last_run_at: datetime | None = None
```

The repository `INVESTIGATIONS` needs no change: `description` is a plain scalar column, picked
up by the generic insert because it is a declared model field outside `_SERVER_GENERATED`.
`title` previously had no bound; the `min_length=1`/`max_length=200` bound is new. Existing rows
all have non-empty titles, so the bound does not retroactively reject stored data; the DB never
re-validates on read (Pydantic validates on `model_validate`, which only runs for rows we read —
and all stored titles are within bound). A reader of a (hypothetical) over-long title would raise
`ValidationError`; that is the same degraded-row path `resources.list` already handles.

### 2. `investigations.open` accepts `description`

`open_investigation(...)` gains `description: str | None = None`, threaded into the
`Investigation(...)` constructor. The tool wrapper advertises an optional `description` param.
A `description` longer than 4096 chars (or a non-`str`) is a `configuration_error` (the existing
`_config_error(project)` boundary catches the `ValidationError`).

### 3. `investigations.set` (new mutating tool, `operator`)

```
investigations.set(investigation_id, title?, description?)
```

- Resolve the operator-owned Investigation (`_resolve_operator_investigation`); not-found →
  `_not_found`.
- Under `advisory_xact_lock(conn, LockScope.INVESTIGATION, uid)`, re-read `FOR UPDATE`
  (`_get_mutable_investigation_locked`): terminal (`closed`/`abandoned`) →
  `_config_error(..., data={"current_status": state})`.
- **At least one of `title`/`description` must be supplied** (both `None` → `_config_error`,
  before taking the lock).
- Partial update: `title` updates only if provided and non-`None`; `description` updates if the
  key is provided. **Distinguish "omit" from "clear":** an omitted `description` leaves the column
  unchanged; an explicit empty string `""` clears it to NULL. Implement with a sentinel so `None`
  on the wire means "omit". (FastMCP passes an unset optional as `None`; an agent clears by
  sending `description=""`.)
- Validate the new `title`/`description` against the model bounds (build the updated
  `Investigation` via `model_copy(update=...)` and `Investigation.model_validate` its dump, or
  validate the individual fields) → `ValidationError` becomes `_config_error`.
- Write with a single `UPDATE investigations SET title = %s, description = %s WHERE id = %s`
  (only the changed columns), audited (`audit.record`, tool `investigations.set`,
  `transition="set"`, args = the set of fields changed — **never the values**, to keep
  free-form text out of the audit log).
- Render through `_envelope_for_investigation(updated)`.

### 4. Surface fields in the rendered envelope

`_envelope_for_investigation(inv)` `data` grows to:

```python
data = {
    "project": inv.project,
    "title": inv.title,
    "description": inv.description,          # may be None
    "external_refs": [r.model_dump() for r in inv.external_refs],
    "state": inv.state.value,
    "last_run_at": inv.last_run_at.isoformat() if inv.last_run_at else None,
}
```

`ToolResponse.data` values are strings elsewhere in this module; confirm the envelope accepts a
nested list/None (it does — `data` is `dict[str, Any]` JSON; verify against `mcp/responses.py`
and the `#404` flat-output-schema sweep so the nested `external_refs` does not reintroduce a
recursive output schema). If `data` is constrained to `str`, JSON-encode the refs list and keep
scalars as strings. **This is a gating check before implementation (see Plan task 0).**

`open_investigation` currently returns a hand-built envelope (`data={"project": project}`); route
its success path through `_envelope_for_investigation(inv)` too, so `open` and `get` report
identically.

### 5. `investigations.list` (new read tool, `viewer`)

```
investigations.list(project?, state?)
```

- Compute `viewer_projects = tuple(projects_with_role(ctx, Role.VIEWER))`.
- If `project` is supplied, it must be in `viewer_projects` (else `_not_found`/empty — match
  `resources.list` visibility: silently exclude, do not raise; an explicit out-of-scope project
  returns an empty collection rather than leaking existence). If omitted, list across all
  `viewer_projects`.
- If `state` is supplied, validate against `InvestigationState` (`configuration_error` on a bad
  value).
- Query: `SELECT * FROM investigations WHERE project = ANY(%s) [AND state = %s] ORDER BY
  created_at DESC` — a focused query in the module (mirrors `resources.py`'s `_fetch_resource_rows`),
  not `list_all` (which is whole-table, unscoped).
- Build one `_envelope_for_investigation` per row into `ToolResponse.collection("investigations",
  "ok", responses, suggested_next_actions=["investigations.get", "investigations.open"])`.
- A row that violates the model invariant is logged and degraded (one error envelope in the
  collection), matching `resources.list`.

## Acceptance criteria

- `investigations.open` with a `description` persists it; `get` returns it.
- `investigations.open` with a 4097-char `description` → `configuration_error` (not a 500).
- `investigations.set` changes `title`; `get` reflects it; audit row recorded with field names
  only.
- `investigations.set` with `description=""` clears the column; with `description` omitted leaves
  it unchanged; with neither field → `configuration_error`.
- `investigations.set` on a `closed`/`abandoned` Investigation → `configuration_error` with
  `current_status`.
- `investigations.set`/`investigations.list` require `operator`/`viewer` respectively; a caller
  without the role is denied by a raised authz error (no `ErrorCategory`).
- `investigations.list` returns only the caller's `viewer`-project rows, newest-first; `state`
  filter narrows; bad `state` → `configuration_error`.
- `investigations.get` `data` includes `title`, `description`, `external_refs` (full), `state`,
  `last_run_at`.
- The two new tools are registered with the right `_docmeta` annotation (`mutating`/`read_only`)
  and `maturity: implemented`, and appear in the generated tool-docs (regenerate + commit).

## Guardrails

`just lint`, `just type`, `just test`. Doc guards: `just check-mermaid`, `just docs-check`,
`just config-docs-check` if tool counts are referenced. Regenerate generated tool docs if the
repo has a generator (`tests/test_tool_docs.py` is the gate — see how it is produced). DB tests
need Docker (`KDIVE_REQUIRE_DOCKER` in CI).

## Out of scope

- Append-only/threaded note history (a future ADR if needed).
- Surfacing `title`/`description` in `usage.investigation` spend reports.
- Free-text search/indexing over descriptions.
