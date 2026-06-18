# Debug session read tools for recovery (#571)

Status: Draft
ADR: [ADR-0176](../adr/0176-debug-session-read-tools.md)

## Problem

The debug surface can `debug.start_session` and `debug.end_session`, and every debug
operation requires a `session_id`, but there is no read tool to discover or inspect a
debug session over MCP. An agent that loses local state after `debug.start_session`
holds a live single-attach transport it can no longer reference: it cannot end it or run
an op against it, and the session blocks a second attach on that System+transport until
the reconciler reaps it. `TOOL_ASSESSMENT.md` finding F7 names this the main
tool-symmetry gap.

## Goals

- `debug.get_session(session_id)` returns one visible debug session.
- `debug.list_sessions` lists the caller's debug sessions, filterable by `run_id`,
  `system_id`, `project`, and session `state`.
- Listing and get do not leak the existence of a session in a project the caller cannot
  view (the no-leak authz boundary, ADR-0123/ADR-0097).
- `runs.get` and `systems.get` surface the ids of active (`attach`/`live`) debug sessions
  the caller already owns, so a recovering agent can pivot from a Run/System it knows to
  the session handle.

## Non-goals

- No new state, transport, or mutation. The read tools never open/close a transport and
  never transition a session.
- No `worker_heartbeat_at` / liveness reinterpretation. State is read as persisted.
- No paging cursor; a clamped `limit` (reusing `DEFAULT_LIST_LIMIT`/`MAX_LIST_LIMIT`)
  matches `systems.list`.

## Design

### Authz boundary

Both read tools require project `VIEWER` (matching `runs.get`, `systems.get`,
`debug.list_breakpoints`), not `OPERATOR`. The existing
`resolve_debug_session_context` helper is `OPERATOR`-gated and is used by the mutating
ops; it is left unchanged. `debug.get_session` uses a viewer-level lookup that resolves
the session and applies the project membership + viewer-role gate directly.

The no-leak rule:

- `debug.get_session` on a malformed id returns `configuration_error`; on a
  syntactically valid id that is absent **or** belongs to a project the caller cannot
  view returns the byte-identical `not_found` envelope — a non-member cannot distinguish
  "no such session" from "exists in a project you can't see".
- `debug.list_sessions` scopes its query to `project = ANY(viewer_projects)`. A `project`
  filter that names a non-member project simply yields zero rows (the project clause is
  intersected with membership, never widened by the filter). An empty membership set
  short-circuits to an empty collection.

### `debug.get_session`

Read the `debug_sessions` row, enforce membership + viewer role, render an envelope:
`object_id = session_id`, `status = <session state>`, `data = {project, run_id,
transport, system_id}` (system_id via the `runs` join, `None` if the run is gone),
`suggested_next_actions` derived from state (`debug.end_session` while
`attach`/`live`, else `debug.get_session`).

### `debug.list_sessions`

Mirror `list_systems`: build SQL clauses from validated filters over a
`debug_sessions s JOIN runs r ON r.id = s.run_id` query, always `AND
s.project = ANY(%s)` with the viewer projects. Filters: `run_id` (uuid),
`system_id` (uuid, matched on `r.system_id`), `project` (string, intersected with
membership), `state` (validated against `DebugSessionState`). Order by
`s.created_at DESC, s.id`; clamp `limit`. Render a `ToolResponse.collection` of
per-session envelopes.

### Run / System surfacing

`runs.get` (`envelope_for_run`) and `systems.get` (`system_envelope`) add an
`active_debug_session_ids` list to `data`: the ids of `attach`/`live` sessions for that
Run (system: for any Run on that System). The Run/System is already project-scoped and
viewer-gated before the envelope is built, so the ids carry no cross-project signal. The
list is empty when there are none. Only the `get` paths take the extra query; the `list`
paths stay one query to avoid an N+1.

## Acceptance criteria

- `debug.get_session` returns one session for a member; `not_found` for absent or
  cross-project; `configuration_error` for a malformed id.
- `debug.list_sessions` returns only the caller's sessions; each filter narrows
  correctly; a cross-project `project` filter yields zero rows; an empty membership
  yields an empty collection.
- A non-member cannot distinguish absent vs. cross-project on either tool.
- `runs.get`/`systems.get` carry `active_debug_session_ids` for owned objects.
- Recovery flow test: start a session, drop the local handle, recover it via
  `list_sessions`/`get_session` (and from `runs.get`), then `end_session`.

## Considered & rejected

- **Reuse the `OPERATOR`-gated `resolve_debug_session_context` for get.** Rejected: a
  read tool should be viewer-visible (symmetry with `runs.get`), and overloading the
  operator helper would either weaken it for the mutating ops or force a viewer caller to
  hold operator. A separate viewer lookup keeps both gates honest.
- **Embed full transport handles in the read envelope.** Rejected: the transport handle
  can carry host/port detail; the recovery flow needs the session id, state, and the
  run/system linkage, not the raw handle. Ops already resolve the handle internally from
  the id.
- **Add session ids to `runs.list`/`systems.list`.** Rejected for now: it is an N+1 on a
  collection path; the `get` paths cover the recovery pivot.
