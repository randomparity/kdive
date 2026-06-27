# Spec: server-side filter parity on `jobs.list` / `allocations.list`

Issue: #621 (`AX_REVIEW.md` A4) · ADR: [ADR-0197](../adr/0197-list-filter-parity.md)
· Builds on [ADR-0192](../adr/0192-list-pagination-envelope.md) (#620, pagination)

## Goal

Give `jobs.list` and `allocations.list` the same server-side filtering `systems.list`
already has, so an agent narrows a result set in one call instead of over-fetching and
filtering client-side against the 200-row cap. All filters are optional and additive — no
existing caller changes.

## Acceptance criteria (from the issue)

1. "my failed build jobs" resolves in a single call with no client-side filtering
   (`jobs.list(status="failed", kind="build")`).
2. "granted allocations" resolves in a single call with no client-side filtering
   (`allocations.list(project=…, state="granted")`).
3. Filters compose with the pagination contract (A3 / ADR-0192): following `next_cursor`
   drains the full *filtered* set with no skip or repeat.

## Contract

### `jobs.list(status=, kind=, investigation_id=, limit=, cursor=)`

New optional parameters (all default `None` = no filter):

- `status: JobState | None` — declared as the typed enum on the FastMCP tool parameter
  (exactly as `systems.list` declares `state: SystemState`). An out-of-enum value is
  rejected at the transport boundary by the framework; the handler binds it as a `state`
  column equality predicate.
- `kind: JobKind | None` — same typed-enum treatment, bound as a `kind` column equality
  predicate.
- `investigation_id: str | None` — a free-form id string (like `systems.list`'s
  `allocation_id`). A malformed UUID → `invalid_uuid` `configuration_error` (ADR-0174,
  via `invalid_uuid_error("investigation_id", …)`). When set, the query joins `runs` on
  `jobs.payload->>'run_id' = runs.id::text` and filters `runs.investigation_id = %s`.

### `allocations.list(project=, state=, limit=, cursor=)`

- `state: AllocationState | None` — typed enum on the tool parameter, bound as a `state`
  column equality predicate. `project` unchanged (still required).

### Composition with pagination (ADR-0192)

Each handler appends its filter predicates to the `WHERE` clause **before** the keyset seek
predicate `(created_at, id) < (%s, %s)` and the `ORDER BY created_at DESC, id DESC LIMIT
limit+1`. The cursor codec, sort key, `truncated`/`next_cursor` derivation, and
`invalid_cursor` handling are unchanged. The filter is re-applied identically on every
page, so the cursor stays a pure boundary and following it never re-admits a filtered-out
row.

### Scoping / no-leak (unchanged)

- `jobs.list` keeps its project predicate `authorizing->>'project' = ANY(readable_projects)`
  on every row. The investigation join does not widen visibility: a caller naming another
  project's investigation id gets no rows (the project predicate still excludes them), so no
  existence leak.
- `allocations.list` keeps `project = %s` + viewer role.

## Implementation

### `jobs.list`

`queue.recent_jobs` gains three optional keyword filters:

```python
async def recent_jobs(
    conn, limit, projects, *,
    after=None,
    status: JobState | None = None,
    kind: JobKind | None = None,
    investigation_id: UUID | None = None,
) -> list[Job]:
```

- `status`/`kind`: append `AND state = %s` / `AND kind = %s` to the `WHERE`, bind the
  `.value`.
- `investigation_id`: add `JOIN runs ON runs.id::text = jobs.payload->>'run_id'` and
  `AND runs.investigation_id = %s`. (An inner join; non-run-bearing jobs have no
  `payload->>'run_id'` so they drop out, which is correct.) The `SELECT` stays `jobs.*`
  (alias the table so `jobs.*` is unambiguous under the join).
- The seek predicate and `ORDER BY`/`LIMIT` are unchanged but reference `jobs.created_at,
  jobs.id` so they stay unambiguous when the join is present.

The `jobs.py` handler `list_jobs` decodes/validates `investigation_id` to a `UUID` (returns
`invalid_uuid_error` on a bad value) before calling `recent_jobs`, and passes
`status`/`kind` straight through (already enum-typed from the registrar). The registrar
`jobs.list` tool declares the new typed-enum + id parameters and threads them in.

### `allocations.list`

`list_allocations` gains `state: AllocationState | None = None`; when set it appends
`AND state = %s` to the existing `WHERE project = %s` before the seek predicate. The
registrar `allocations.list` tool declares `state: AllocationState | None = None` and
threads it in.

## Edge cases (each gets a test)

`jobs.list`:
- **`status` filter** → only jobs in that state; combined with pagination the cap applies to
  the matching set.
- **`kind` filter** → only jobs of that kind.
- **`status` + `kind`** → conjunction (e.g. failed builds only).
- **`investigation_id` filter** → only the run-bearing jobs whose Run is in that
  investigation; a provision/teardown job (no `run_id`) is excluded even if it touched a
  System the investigation used.
- **`investigation_id` malformed UUID** → `invalid_uuid` `configuration_error`.
- **`investigation_id` for another project's investigation** → empty page (project
  predicate excludes the rows; no leak), not an error.
- **`investigation_id` with no matching jobs** → empty page, `truncated=false`.
- **filter + cursor drain** → following `next_cursor` reads every matching row exactly once,
  no skip/repeat across a shared-`created_at` tie boundary.
- **no filters** → unchanged behavior (existing tests stay green).

`allocations.list`:
- **`state` filter** → only allocations in that state (e.g. `granted`).
- **`state` + cursor drain** → full filtered set across pages, no skip/repeat.
- **`state` with no matches** → empty page, `truncated=false`.
- **no filter** → unchanged behavior.

Both: an out-of-enum `status`/`kind`/`state` token is rejected by the framework at the
boundary (covered where the project tests transport-level validation, else asserted via the
typed signature — the handler never receives an invalid enum).

## Non-goals

- `runs.list` (#623, sibling) — different files; not in scope.
- `systems.list` filters — already exist (ADR-0070/0180); unchanged.
- No new schema column, migration, tool, or top-level envelope field. No change to the
  ADR-0192 cursor codec or sort keys.
- No new free-form-string validation path: enum filters use the typed-parameter ergonomics,
  not the `config_error_reason` + `accepted_values` handler path (which `systems.list`
  retains only for its historical `state` string-arg shape).

## Guardrails

`just lint`, `just type` (whole tree), `just test` per the justfile; `just ci` before push.
CI also gates `just docs-check` and `just adr-status-check` **individually** (not via
`just ci`), so after editing the `jobs.list`/`allocations.list` tool docstrings or
parameters run `just docs` and commit the regenerated `docs/guide/reference/*.md`. Tests
live beside the existing list-tool tests (`tests/mcp/jobs/test_jobs_tools.py`,
`tests/mcp/lifecycle/test_allocations_tools.py`, `tests/jobs/test_queue.py`).
