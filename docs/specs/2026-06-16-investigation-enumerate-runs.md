# Spec — Investigation enumerates its attached runs/systems (#488)

- **Issue:** [#488](https://github.com/randomparity/kdive/issues/488) (black-box MCP eval, D8)
- **ADR:** [0143](../adr/0143-investigation-enumerate-runs.md)
- **Date:** 2026-06-16

## Problem

`investigations.get` does not enumerate the Runs grouped under the Investigation. An
Investigation exists to group Runs across Allocations, but the response carries no `runs[]`,
so a caller must track Run ids out-of-band or re-derive them with `runs.list()` filtered in
memory by `investigation_id`. There is no investigation-scoped run navigation over the tool
surface.

The relationship already exists: `Run.investigation_id` is a non-null FK to `investigations`
(`db/schema/0001_init.sql`).

## Acceptance

`investigations.get(investigation_id)` returns the ids of the Runs grouped under it, so a
caller navigates investigation → runs without out-of-band bookkeeping.

## Design

Enrich `_envelope_for_investigation` — the single render path for `get`, `open`, `close`,
`link`, `unlink`, `set`, and each `list` item — with two `data` fields:

- `runs: list[str]` — ids of every `Run` with this `investigation_id`, ordered `created_at,
  id` (oldest first, stable).
- `systems: list[str]` — the distinct `system_id`s those runs touched, in first-seen order.

Resolution: one query, `SELECT id, system_id FROM runs WHERE investigation_id = %s ORDER BY
created_at, id`, issued on the connection that already read and authorized the Investigation
row. A Run's project equals its Investigation's (enforced at `runs.create`), and the
Investigation row was already resolved under the caller's `viewer` scope, so no extra project
predicate is needed.

`_envelope_for_investigation` becomes `async def` and takes the open `conn`. Every call site
already runs inside `async with pool.connection() as conn`, so each threads its connection
through; the mutators call it after their own write commits (or on the idempotent read path),
so the enumeration reflects the post-write state.

Ids only — light refs, not embedded `Run` objects (the "references, never log dumps"
invariant). The caller follows up with `runs.get` for detail.

### Edge cases

- **No runs yet** (freshly-`open`ed Investigation): `runs: []`, `systems: []`. The honest
  empty answer, not an error.
- **Multiple runs on one system:** `systems` deduplicates, preserving first-seen order.
- **`list` over N Investigations:** one runs query per item (N+1). Accepted — `list` is a
  cold reporting path over small pages; ADR-0143 records the deferral.

## Out of scope

- A standalone `runs.list(investigation_id=...)` scoped filter (a separate, additive surface;
  not a prerequisite for the issue's navigation acceptance).
- Embedding full `Run` objects or any run detail beyond the id.
- Any schema change or migration (FK + index already exist).

## Testing

- `get` on an Investigation with two runs on two systems returns both run ids (ordered) and
  both system ids.
- Two runs on the *same* system collapse to one entry in `systems`.
- A freshly-`open`ed Investigation returns empty `runs`/`systems`.
- The enriched fields appear on the `open`, `close`, and `list`-item envelopes (single render
  path), and runs from *another* Investigation in the same project are excluded.
