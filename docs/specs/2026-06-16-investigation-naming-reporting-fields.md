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

    title: str
    description: str | None = None
    external_refs: list[ExternalRef] = Field(default_factory=list)
    state: InvestigationState
    last_run_at: datetime | None = None
```

**Length bounds are enforced at the write boundary, not on the model field** (ADR-0135;
adversarial-review finding 1). `model_validate` runs on the **read** path — the repository
deserializes every row through it (`repositories.py` `get`/`list_all`). `title` was previously a
bare `title: str` (`domain/models.py:297`) with no bound, and the DDL is `title text NOT NULL`
(an empty `''` title is storable). Putting `min_length`/`max_length` on the field would therefore
be enforced when **loading** a pre-existing row, so any already-persisted over-long or empty title
would become unreadable (`get`/`list`/`set` all raise). To avoid retroactively breaking reads, the
field stays permissive and a shared `_validate_text(title, description)` boundary check
(`1..=200` chars for a supplied `title`, `0..=4096` for a supplied `description`) runs in
`open`/`set` and returns `configuration_error` on violation.

`description` is safe to *also* bound on the column: it is a brand-new column, so no existing row
can violate it, and the DB `CHECK (char_length(description) <= 4096)` blocks any over-long write.
The boundary check is the primary control; the DB `CHECK` is defence-in-depth.

The repository `INVESTIGATIONS` needs no change: `description` is a plain scalar column, picked
up by the generic insert because it is a declared model field outside `_SERVER_GENERATED`.

### 2. `investigations.open` accepts `description`

`open_investigation(...)` gains `description: str | None = None`, threaded into the
`Investigation(...)` constructor. The tool wrapper advertises an optional `description` param.
The boundary `_validate_text` check rejects a `title`/`description` over its bound with
`configuration_error`. **Empty-string normalization (finding 2):** a `description` of `""` is
normalized to `None` on `open` (and on `set`, below), so `description` is either a non-empty
string or NULL everywhere — never a literal `""`. This keeps `open` and `set` consistent.

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
- **Omit vs. clear is value-based, not a Python sentinel (finding 2).** Over MCP/JSON an omitted
  optional and an explicit `null` are indistinguishable at the FastMCP boundary — both arrive as
  `None` — so `None` for either field means **leave unchanged**. `description=""` is the **clear**
  signal: it normalizes to `NULL`. `title` cannot be cleared (it is `NOT NULL`); `title=""` (or
  any title under the 1-char bound) is a `configuration_error`. So:
  - `title=None` → leave `title` unchanged; a non-empty `title` (≤200 chars) → update.
  - `description=None` → leave `description` unchanged; `description=""` → set NULL; a non-empty
    `description` (≤4096 chars) → update.
- Validate supplied fields with the shared `_validate_text` boundary check → `configuration_error`
  on a bound violation. (Do **not** validate via whole-model `model_validate`: that would re-check
  the *existing* stored `title`, reintroducing finding 1's read hazard for a `set` that only
  edits `description`.)
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
identically. **Intended contract change (finding 3):** today `open` advertises
`suggested_next_actions=["investigations.get", "runs.create"]`; the shared helper advertises
`["investigations.get", "investigations.close", "runs.create"]` for a non-terminal Investigation.
After this change `open` also suggests `investigations.close` — accepted as intended (a fresh
Investigation can legitimately be closed), and the `open` test is updated to assert the new set.

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
  created_at DESC, id DESC` — a focused query in the module (mirrors `resources.py`'s
  `_fetch_resource_rows`), not `list_all` (which is whole-table, unscoped). The `id DESC` tiebreak
  makes "newest-first" deterministic when two rows share a `created_at` (finding 4). Pass the
  project list as a Python list bound to `ANY(%s)`; an empty `viewer_projects` yields an empty
  collection (matches nothing) rather than an error.
- Build one `_envelope_for_investigation` per row into `ToolResponse.collection("investigations",
  "ok", responses, suggested_next_actions=["investigations.get", "investigations.open"])`.
- A row that violates the model invariant is logged and degraded (one error envelope in the
  collection), matching `resources.list`.

## Acceptance criteria

- `investigations.open` with a `description` persists it; `get` returns it.
- `investigations.open` with a 4097-char `description` → `configuration_error` (not a 500); a
  201-char `title` → `configuration_error`; `title=""` → `configuration_error`.
- `investigations.open` with `description=""` persists `NULL` (not a literal `""`).
- **Regression (finding 1):** an Investigation whose stored `title` exceeds 200 chars (writable
  before this change, since `title` was unbounded) is still readable by `get`/`list`/`set` — the
  bound lives at the write boundary, not on the model field. A unit test inserts such a row
  directly and asserts `get` succeeds.
- `investigations.set` changes `title`; `get` reflects it; audit row recorded with field names
  only.
- `investigations.set` with `description=""` clears the column to `NULL`; with `description`
  omitted (or `None`) leaves it unchanged; with neither field → `configuration_error`; with a
  201-char `title` → `configuration_error`.
- `investigations.set` on a `closed`/`abandoned` Investigation → `configuration_error` with
  `current_status`.
- `investigations.set`/`investigations.list` require `operator`/`viewer` respectively; a caller
  without the role is denied by a raised authz error (no `ErrorCategory`).
- `investigations.list` returns only the caller's `viewer`-project rows, newest-first (with a
  deterministic `id` tiebreak); `state` filter narrows; bad `state` → `configuration_error`; a
  caller with no `viewer` projects gets an empty collection.
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
