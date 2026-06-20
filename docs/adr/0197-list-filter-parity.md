# ADR-0197: Server-side filter parity on `jobs.list` / `allocations.list`

Status: Accepted

## Context

`systems.list` filters server-side by `allocation_id`, `state`, `shape`, and a PCIe match
(ADR-0070, ADR-0180), so an agent narrows the result set in one call. The other two
single-stream lifecycle lists are limit-only: `jobs.list` takes only `limit`/`cursor` and
`allocations.list` takes `project`/`limit`/`cursor` (issue #621, `AX_REVIEW.md` A4). An
agent that wants "my failed build jobs" or "granted allocations" must over-fetch and filter
client-side, which is wasteful and easy to get wrong against the 200-row cap — a `failed`
job past the first 200 rows is invisible to a client-side filter, while a server-side
`status="failed"` filter caps the *matching* set.

Both lists were just given keyset pagination (ADR-0192, #620, now on `main`). The cursor is
a sort-key boundary, not a result-set snapshot; every page re-applies the same project/role
`WHERE` clause. A filter must therefore be applied identically on every page, before the
keyset seek predicate, so the cursor stays a pure boundary and following it never re-admits
a filtered-out row.

`jobs.list` reads through `queue.recent_jobs`, scoped to the caller's readable projects via
`authorizing->>'project'`. The `jobs` table carries `kind` and `state` columns directly, so
those two filters are local column predicates. It carries **no** `investigation_id` or
`run_id` column: a job references a Run only through its `payload->>'run_id'`, and only the
run-bearing kinds (`build`, `install`, `boot`) carry one (`_RUN_PAYLOAD_MODELS` in
`jobs/payloads.py`). A Run carries `investigation_id`. So filtering jobs by investigation is
a join `jobs.payload->>'run_id' = runs.id::text` then `runs.investigation_id = %s` — there
is no direct column.

`allocations.list` reads `allocations` scoped to one `project`; `state` is a local column.

## Decision

Mirror `systems.list`'s filter ergonomics on the two lists. All filters are optional and
additive — absent means "no filter", so every existing caller is unchanged. No schema
change, no migration, no new tool.

### `jobs.list(status=, kind=, investigation_id=)`

- `status: JobState | None` and `kind: JobKind | None` are declared as the typed enums on
  the FastMCP tool parameter (as `systems.list` declares `state: SystemState`), so a value
  outside the enum is rejected at the transport boundary with the framework's validation
  error; a `None` means no filter. The handler binds `status`/`kind` as equality predicates
  on the `state`/`kind` columns.
- `investigation_id: str | None` is a free-form id string (like `systems.list`'s
  `allocation_id`); a malformed UUID is an `invalid_uuid` `configuration_error` (ADR-0174).
  When set, the query joins `runs` on `jobs.payload->>'run_id' = runs.id::text` and filters
  `runs.investigation_id = %s`. Because only `build`/`install`/`boot` jobs carry a
  `run_id`, the filtered set is exactly the run-bearing jobs of that investigation;
  provision/teardown/image-build/diagnostics jobs (no `run_id`) never match an
  `investigation_id` filter, which is correct — they are not part of any investigation.
- The investigation join does **not** widen visibility. The existing project predicate
  (`authorizing->>'project' = ANY(readable_projects)`) still gates every row, so a caller
  cannot read another project's jobs by naming that project's investigation id; an
  investigation id the caller cannot see simply yields no rows (no existence leak, matching
  the by-id read contract).

### `allocations.list(state=)`

- `state: AllocationState | None` declared as the typed enum on the tool parameter; the
  handler binds it as an equality predicate on the `state` column. The `project` predicate
  is unchanged.

### Composition with pagination

Each handler appends its filter predicates to the `WHERE` clause **before** the ADR-0192
keyset seek predicate and the `ORDER BY … LIMIT limit+1`. The cursor codec, sort key,
`truncated`/`next_cursor` derivation, and `invalid_cursor` handling are unchanged: the
cursor remains the `(created_at, id)` boundary and the filter is re-applied on every page,
so following `next_cursor` reads the full *filtered* set with no skip or repeat.

## Consequences

- "my failed build jobs" resolves in one call (`jobs.list(status="failed", kind="build")`)
  and "granted allocations" in one (`allocations.list(project=…, state="granted")`), each
  capped on the matching set rather than over-fetched and filtered client-side. Following
  `next_cursor` drains the whole matching set.
- An invalid enum value fails loud at the boundary (framework validation), and a malformed
  `investigation_id` is a self-correcting `invalid_uuid` `configuration_error`; neither is a
  silent empty page.
- `queue.recent_jobs` grows three optional keyword filters; its existing callers
  (`jobs.list` pagination, the unfiltered path) keep today's behavior with the filters
  defaulted off. The investigation filter adds a `LEFT`/inner join to `runs` only when
  `investigation_id` is supplied — the common unfiltered/kind/status paths keep the
  single-table scan.
- No new redaction surface: the filters echo only the caller-supplied enum token or id, and
  the response envelopes are unchanged.

## Considered & rejected

- **An `investigation_id` / `run_id` column on `jobs`.** A migration and a write-path change
  to denormalize a value already reachable through `payload->>'run_id'` → `runs`. The join
  is only taken when the filter is supplied, and the issue is additive; a schema change is
  disproportionate.
- **Filtering jobs by investigation client-side or in Python after the fetch.** Defeats the
  purpose: a matching job past the cap would be invisible, exactly the over-fetch problem
  the issue calls out. The filter must be in the SQL `WHERE` so the cap applies to the
  matching set.
- **A free-form `status`/`kind`/`state` string validated in the handler** (the
  `config_error_reason` + `accepted_values` pattern `systems.list` uses for `state`).
  Declaring the typed enum on the tool parameter (as `systems.list` does for its
  `SystemState` param) is the same ergonomics with less code: the framework rejects an
  unknown token before the handler runs and the generated tool reference lists the accepted
  values from the enum. `investigation_id` stays a free-form id for the same reason
  `allocation_id` does — it is an opaque UUID, not a closed vocabulary.
- **Returning jobs of every kind for an `investigation_id` filter** (e.g. also the
  System-scoped provision job of a System a Run used). A job belongs to an investigation
  only through its Run; provision/teardown are System-lifecycle jobs with no Run and no
  investigation. Including them would require a second, ambiguous join path and would report
  jobs that are not part of the investigation.
- **A `>1` / `0`-filter discriminated-union selector** like the allocation request target
  (ADR-0186). The three job filters are independent and freely combinable (status AND kind
  AND investigation), not mutually exclusive, so a "exactly one of" union is wrong here.
