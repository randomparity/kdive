# ADR 0143 — Investigation enumerates its attached runs/systems

- **Status:** Proposed <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-16
- **Deciders:** KDIVE maintainers

## Context

An `Investigation` (ADR-0026) exists to group `Run`s across Allocations toward one debugging
goal. The relationship is already modeled: `Run.investigation_id` is a non-null FK to
`investigations` (`db/schema/0001_init.sql`). But `investigations.get` returns only
`project`, `title`, `description`, `external_refs`, `state`, and `last_run_at` — never the
runs grouped under the Investigation. An agent that opens an Investigation, creates several
Runs against it, and later re-reads the Investigation has no way to navigate back to those
Run ids: the only path is `runs.list()` filtered in memory by `investigation_id`, and the
caller must have tracked the ids out-of-band. Found during black-box MCP evaluation (#488,
call #37, D8).

The constraint is the cross-cutting invariants in `AGENTS.md`: a uniform `ToolResponse`
envelope, project-scoped reads (`viewer` on the owning project), and "references, never log
dumps" — the response stays a small list of ids, not embedded `Run` objects.

## Decision

`investigations.get` (and therefore `_envelope_for_investigation`, which `open`/`close`/the
mutators all render through) gains two `data` fields:

1. **`runs: list[str]`** — the ids of every `Run` whose `investigation_id` is this
   Investigation, ordered `created_at, id` (oldest first, stable).
2. **`systems: list[str]`** — the distinct `system_id`s those runs touched, in first-seen
   order over the same ordered run set.

Both are resolved by one query — `SELECT id, system_id FROM runs WHERE investigation_id = %s
ORDER BY created_at, id` — issued on the same connection that already read (and authorized)
the Investigation row. No extra project predicate is needed: a Run's project equals its
Investigation's project (enforced at `runs.create`), and the Investigation row was already
resolved under the caller's `viewer` scope, so the runs it groups are in-scope by
construction.

Ids only (light refs), not full `Run` objects: the caller follows up with `runs.get` for
detail. This keeps the envelope small and avoids an N-object embed that would re-fail the
"references, never log dumps" rule. `_envelope_for_investigation` becomes `async` because it
now issues a query; every caller already runs inside an `async with pool.connection()` block,
so each passes its open connection through.

No schema change, no migration: the FK and index already exist. The advertised tool input
schema is unchanged (the flat `{"type":"object"}` of ADR-0113); only the response `data`
shape grows, which is additive and backward compatible for any existing reader.

## Consequences

- A reader of any Investigation envelope (`get`, `open`, `close`, `link`, `unlink`, `set`,
  and each `list` item) now sees `runs`/`systems`. `list` therefore issues one runs query per
  Investigation (N+1 over the page). Investigation pages are small and `list` is a cold
  reporting path, so this is acceptable; if it ever matters, the per-item enumeration can move
  behind a `get`-only flag in a later ADR.
- An Investigation with no runs yet returns `runs: []` / `systems: []` (the common
  freshly-`open`ed case), which is the honest empty answer, not an error.
- The two lists are a point-in-time snapshot read under the same connection as the
  Investigation row; they are not transactionally pinned against a concurrent `runs.create`,
  which is the same read-consistency contract every other read tool already has.

## Considered & rejected

- **Embed full `Run` objects.** Rejected: violates "references, never log dumps", bloats the
  envelope, and duplicates `runs.get`. Ids let the caller fetch only what it needs.
- **A separate `runs.list(investigation_id=...)` filter instead of enriching `get`.** A
  scoped runs filter is useful on its own, but the issue's acceptance is navigation *from the
  Investigation*, and enriching the envelope the agent already holds is the smaller surface.
  A scoped runs filter can still be added later; it is not a prerequisite.
- **`systems` only, no `runs`.** Rejected: the issue's core ask is the runs; systems are the
  "optionally include" extra. Both are one query, so we return both.
- **Add a project predicate to the runs query.** Rejected as redundant: the Investigation is
  already authorized and a Run's project equals its Investigation's. The extra predicate would
  imply runs could be cross-project under one Investigation, which the data model forbids.
