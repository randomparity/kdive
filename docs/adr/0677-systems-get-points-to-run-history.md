# 0677 — systems.get points to a System's run history

## Status

Accepted (2026-09-23)

## Context

ADR-0180 gave `systems.get` recovery context, including `active_run`: the newest Run on the
System that is neither `failed` nor `canceled`. A System can outlive the Investigation that
provisioned it and host Runs from later Investigations (#2689). From the System, an agent
cannot find those Runs or Investigations. `investigations.get` already lists its Runs and
Systems (#488); the reverse direction has no pointer.

## Decision

`systems.get` returns the System's own `investigation_id`, the distinct investigation ids of
its Runs as `run_investigation_ids` (every Run state, newest first, capped at 20, with a
`run_investigation_ids_truncated` flag), and appends `runs.list` to its next actions, after
ADR-0454's recovery actions on a `failed` System. `runs.list(system_id=…)` stays the one paginated read of the Runs themselves.
`investigation_id` also appears on `systems.list` items because it is a row column; the
investigation list stays get-only.

## Consequences

- An agent can get from a System to every Investigation that used it in one call, and to its
  Runs through `runs.list`.
- `systems.get` makes one more query, a scan of `runs` by `system_id` like `active_run`'s.
- The `systems.get` tool description (`systems/registrar.py`) does not yet name the new keys;
  the toolset guide does.
- More than 20 Investigations on one System read as `run_investigation_ids_truncated: true`;
  the rest are reachable through `runs.list`.

## Considered & rejected

- **Do nothing; document `runs.list(system_id=…)` only.** judgment: the investigation ids are
  the missing link the issue reports, and a guide line alone leaves the envelope silent.
- **Embed a recent-runs summary (id, state, investigation_id) with a count.** judgment: it
  duplicates `runs.list`, and the operator excluded paginated history inside `systems.get`.
- **Return the full, unbounded investigation list.** judgment: a long-lived System would grow
  the envelope without limit; the cap plus `runs.list` covers the tail.
- **Add the list to `systems.list` items.** judgment: a per-item query, the N+1 ADR-0180
  rejected for `active_run`.
