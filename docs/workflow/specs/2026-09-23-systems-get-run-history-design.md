# systems.get run history pointers

Issue: #2689. Decision: [ADR-0677](../../adr/0677-systems-get-points-to-run-history.md).
Base: `main`. Lane: full-spec.

## Problem

`systems.get` shows only `active_run`: the newest Run on the System that is not `failed` or
`canceled`. It omits the System's own `investigation_id`, and no response or guide text points
to `runs.list(system_id=…)`. When an Investigation reuses a System that an earlier one
provisioned, an agent cannot find the earlier Runs or Investigations from the System.

## Requirements

1. The `systems.get` and `systems.list` item envelopes carry `data.investigation_id`: the
   System row's `investigation_id` as a string, or `null` for a System with no owning
   Investigation. It is a row column, so the list path gains no query (ADR-0180).
2. `systems.get` carries `data.run_investigation_ids`: the distinct `investigation_id`s of the
   Runs whose `system_id` is this System, in every Run state, newest first by each
   Investigation's newest Run `created_at` (an Investigation with several Runs appears once),
   ties broken by `investigation_id`, at most
   `RUN_INVESTIGATIONS_LIMIT = 20` entries.
3. `systems.get` carries `data.run_investigation_ids_truncated`: `true` exactly when more than 20
   such Investigations exist.
4. `systems.get` appends `runs.list` as the last next action, on the success envelope
   (`["systems.get", "systems.teardown", "runs.list"]`) and on the `failed` envelope (after the
   ADR-0454 recovery actions, whose order and static lists stay unchanged). `systems.list`
   items, which carry no history, keep their current actions.
5. The systems toolset guide (`docs/guide/toolsets/systems.md`, and its generated MCP resource
   snapshot `src/kdive/mcp/resources/_content/toolsets-systems.md`, ADR-0151) says to call
   `runs.list(system_id=…)` for a System's full run history.
6. Unchanged: `active_run`, every other field, the list path's single query, and the
   not-found and role checks, which run before the new query.

## Design

Owner: `src/kdive/mcp/tools/lifecycle/systems/view.py`, which already owns `active_run`.

- `system_envelope` adds `"investigation_id"` to `data` from `system.investigation_id`.
- A frozen dataclass `SystemRunHistory(investigation_ids: list[str], truncated: bool)`.
- `_run_history_for_system(conn, system_id, project) -> SystemRunHistory` runs one query:
  `SELECT investigation_id FROM runs WHERE system_id = %s AND project = %s GROUP BY
  investigation_id ORDER BY max(created_at) DESC, investigation_id LIMIT %s` with
  `RUN_INVESTIGATIONS_LIMIT + 1`, then trims with the existing `paginate` helper. The
  `project` predicate restates the admission invariant (`runs/admission.py` and `runs/bind.py`
  refuse a Run whose project differs from its System's), so a violated invariant cannot leak
  another project's ids.
- `system_envelope` takes `run_history: SystemRunHistory | None = None`. When set, it adds the
  two keys and appends `runs.list` to whichever action list the envelope returns (success or
  `failed`). Only `get_system` passes it.

## Failure model

1. **Actors and deployments** — an authenticated MCP agent with the viewer role on the
   System's project; every deployment that serves `systems.get`.
2. **Invariants and assets at stake** — tenancy: no investigation id from a project the caller
   cannot read; the published `systems.get`/`systems.list` response shape, changed additively.
3. **Accepted failure classes**
   - `runs` has no `system_id` index, so the query scans `runs`; `_active_run_for_system`
     already does the same scan on this path. Bounded cost; an index needs a migration, which
     the dispatch excludes.
   - The ids are a snapshot; a Run created after the read is missing until the next read.
4. **Covered elsewhere** — full paginated history: `runs.list` (ADR-0198). Other
   Investigations' titles and states: `investigations.get`.
