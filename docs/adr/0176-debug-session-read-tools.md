# ADR-0176: Debug session read tools for recovery

Status: Accepted

## Context

The `debug.*` surface can `start_session`/`end_session` and run ops keyed by
`session_id`, but exposes no read tool to discover or inspect a session (`sessions.py`
registrar; `TOOL_ASSESSMENT.md` finding F7, #571). An agent that loses local state after
`debug.start_session` holds a live single-attach transport it can no longer name: it
cannot end it or operate it, and the session blocks a second attach on that
System+transport until the reconciler reaps it. The read surface is otherwise
symmetrical (`runs.get`/`runs.list`, `systems.get`/`systems.list`).

## Decision

Add two viewer-gated read tools and surface active session ids on the owning objects.

1. **`debug.get_session(session_id)`** — return one debug session the caller's project
   owns. Requires project `VIEWER`. A malformed id is `configuration_error`; a valid id
   that is absent or in a non-member project is the byte-identical `not_found` envelope
   (no cross-project existence leak, ADR-0097/ADR-0123). The envelope carries
   `{project, run_id, transport, system_id}` and a state-derived `suggested_next_actions`.

2. **`debug.list_sessions`** — list the caller's debug sessions, filterable by `run_id`,
   `system_id`, `project`, and `state`. Requires `VIEWER`. The query is always scoped
   `s.project = ANY(viewer_projects)` over `debug_sessions s JOIN runs r ON r.id =
   s.run_id`; the `project` filter is intersected with membership (never widened), so a
   cross-project `project` value yields zero rows and an empty membership short-circuits
   to an empty collection. Mirrors `systems.list` (clamped `limit`, `created_at DESC`
   order, `ToolResponse.collection`).

3. **Run/System surfacing** — `runs.get` and `systems.get` add
   `active_debug_session_ids` (the `attach`/`live` session ids for that Run, or for any
   Run on that System) to `data`. The object is already project-scoped and viewer-gated
   before the envelope is built, so the ids carry no cross-project signal. Only the `get`
   paths take the extra query; the `list` paths stay single-query (no N+1).

The tools register on the existing debug registrar, are classified `_VIEWER` in
`exposure.py`, and are listed in `test_tool_docs.py`. No schema change, no migration.

## Consequences

- A recovering agent can re-discover a live session from the session id, from a Run, or
  from a System, then `end_session` or operate it — closing the F7 symmetry gap.
- The read tools never open/close a transport or transition a session; session `state`
  is read as persisted (no liveness reinterpretation).
- The viewer-level get path does not reuse the `OPERATOR`-gated
  `resolve_debug_session_context` (kept for the mutating ops), so neither gate is
  weakened.

## Considered & rejected

- **Reuse the `OPERATOR`-gated `resolve_debug_session_context` for get.** A read tool
  should be viewer-visible; overloading the operator helper would weaken it for the
  mutating ops or force viewers to hold operator.
- **Embed the raw transport handle in the read envelope.** The handle can carry
  host/port detail and is not needed for recovery; ops resolve it internally from the id.
- **Add session ids to `runs.list`/`systems.list`.** An N+1 on a collection path; the
  `get` paths cover the recovery pivot.
