# ADR-0198: Viewer-gated `runs.list` read tool

Status: Accepted

## Context

`runs.*` registers nine tools but no list (issue #623, `AX_REVIEW.md` A2). A Run is
reachable only one-at-a-time by id (`runs.get`) or indirectly per-investigation. There is
no way to answer "which Runs are on this System?" or "what is building across my project?"
in one call.

Every other lifecycle family already exposes a viewer-gated, no-leak `list`:
`systems.list` (ADR-0070/0180), `allocations.list`, `investigations.list`, `jobs.list`.
Each clamps a `limit`, filters by the caller's granted projects, and — since ADR-0192 —
opt-in keyset-paginates over `(created_at, id) DESC` through the open `ToolResponse.data`
payload (`truncated` / `next_cursor`, opaque tool-tagged `cursor`).

The `runs` table carries every filter axis as a direct column: `project` (no join needed
for scoping), `system_id`, `investigation_id`, `state`. Unlike `systems.list`, no
allocation/resource join is required for the requested filters; the existing
`envelope_for_run` already renders a Run row to the standard per-item envelope.

## Decision

Add a read-only, viewer-gated `runs.list` filterable by `system_id`,
`investigation_id`, and `state`, returning the standard per-item Run envelope and the
ADR-0192 pagination payload. It mirrors `systems.list` exactly, simplified to the
no-join case:

- **Scoping (no-leak).** The query restricts to `project = ANY(<viewer projects>)`, where
  viewer projects are the caller's member projects that hold any granted role
  (`ctx.roles.get(p) is not None`), exactly as `systems.list`. A Run in an ungranted or
  non-member project is invisible — never a membership leak. An empty viewer-project set
  short-circuits to an empty collection.
- **Filters.** `system_id` and `investigation_id` parse as UUIDs (a malformed value is a
  `configuration_error` `invalid_uuid` naming the field, ADR-0174); `state` resolves
  against `RunState` (an unknown value is a `configuration_error` `invalid_state`
  enumerating the accepted values). Each supplied filter is an additional `AND` clause.
- **Pagination.** Keyset over `(created_at, id) DESC` using the shared
  `encode_ts_uuid_cursor` / `decode_ts_uuid_cursor` helpers with tool tag `runs.list`,
  `limit` defaulting to 50 and clamped to 200, `truncated` computed by the fetch-`limit+1`
  `paginate` helper, and `next_cursor` present iff `truncated`. A malformed or wrong-tool
  cursor is a `configuration_error` `invalid_cursor`, never a silent first-page fallback.
- **Per-item shape.** Each row renders through the existing `envelope_for_run` with no
  get-only enrichment: `runs.get`'s N+1 lookups (system/runtime-derived `required_cmdline`,
  the linked failing job's redacted reason, `active_debug_session_ids`, install/boot
  `step_progress`) are omitted on the list path, matching how `systems.list` omits
  `active_run` / `active_debug_session_ids`. A `failed` Run still renders as a failure-shaped
  item carrying its `failure_category` (the no-job-derived reason is resource-free, #516), so
  no cross-project signal leaks through the failure surface.
- **Registration.** `runs.list` is registered in the `runs` registrar, classified
  `_VIEWER` in `exposure.py` (the `CLASSIFIED_TOOLS` map, the `debug.list_sessions`
  precedent), and mapped to its test module in the ADR-0047 docs guard.

## Consequences

- "Runs on System X" and "Runs building in my project" each resolve in one call, filtered
  and paginated, with the same no-leak boundary as the rest of the lifecycle surface.
- No schema or migration change: every filter and sort column already exists on `runs`.
- The list path deliberately under-populates each item relative to `runs.get`. A caller
  that needs a Run's `required_cmdline`, failing-job reason, live debug sessions, or
  install/boot step map calls `runs.get` for that one Run (the same get-vs-list split
  ADR-0180 established for systems).

## Considered & rejected

- **Enrich each list item like `runs.get`.** Rejected: per-row system/runtime resolution,
  failing-job fetch, debug-session lookup, and step-progress query are an N+1 per page.
  `systems.list` set the precedent of a lean list item + a rich `get`; `runs.list` follows
  it.
- **A `target_kind` filter.** Rejected as out of the issue's scope (it names
  `system_id` / `investigation_id` / `state`). `target_kind` is a column and a later ADR
  can add it without reshaping the tool; adding it now would be a speculative axis.
- **Offset/limit paging or a one-off paging shape.** Rejected: ADR-0192 already settled
  keyset pagination through the shared cursor helpers for the whole list surface; a new
  tool must use them, not reinvent paging.
- **A new list module under `runs/`.** The `runs/` lane is one module per verb
  (`view.py`, `create.py`, `bind.py`, …); the list handler lives in a new `list.py`
  sibling for symmetry, not folded into `view.py`.
