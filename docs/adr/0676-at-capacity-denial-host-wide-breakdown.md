# 0676 — Host-wide breakdown and resources.availability breadcrumb on the at_capacity denial

## Status

Accepted (2026-09-23)

## Context

`allocations.request`'s `at_capacity` denial reports `in_use` as a bare number
(`host capacity exhausted (cap {cap}, in use {in_use})`). `_count_occupying`
(`services/allocation/admission/core.py`) counts every occupying allocation on the host
(`GRANTED`/`ACTIVE`/`RELEASING`) across **all** projects, but `allocations.list` — the denial's
only recourse (ADR-0132, ADR-0245) — returns only the caller's readable projects. An agent
denied at cap 4 with `in_use` 4 who sees 2 allocations of their own on `allocations.list` has no
way to reconcile the numbers, and no pointer to `resources.availability`, which applies the same
occupancy predicate fleet-wide (#2687; reported from the 0.5.0 ppc64le validation, where the two
unseen slots were another project's leaked allocations, #2686).

ADR-0132 decision 2 and ADR-0245's Decision both state that the host-capacity path "keeps
`[\"allocations.list\"]`" unchanged. This ADR amends that for the `at_capacity` reason only,
recording the decision `allocations.request at_capacity denial: say in_use is host-wide, break it
down, point to resources.availability` (#2687) actually made. Neither ADR's decision for any
other reason (affinity, budget, quota, generic) changes.

## Decision

For the `at_capacity` reason only:

1. **The detail states the count is host-wide.** `_denial_detail`
   (`mcp/tools/lifecycle/allocations/request.py`) rewords the prose to `host capacity exhausted
   (cap {cap}, in use {in_use} host-wide across all projects)`.
2. **The denial payload carries a disclosure-safe breakdown.** `_host_cap_check`
   (`services/allocation/admission/core.py`) computes `_OccupancyBreakdown` — the same
   `OCCUPYING_ALLOCATION_STATES` query, grouped by state and by whether the row's project matches
   the requesting project — and the denial's `details` dict carries three new keys:
   `in_use_own_projects`, `in_use_other_projects` (both `int`), and `in_use_by_state` (a
   `dict[str, int]` keyed by state value). No other project's name or id is ever included; only
   aggregate counts cross the boundary. `in_use_own_projects + in_use_other_projects ==
   in_use` and `sum(in_use_by_state.values()) == in_use` always hold, because both are derived
   from the same query result as the existing `in_use` total.
3. **`suggested_next_actions` leads with `resources.availability`.** A new
   `_HOST_CAP_NEXT_ACTIONS = ["resources.availability", "allocations.list"]` replaces the plain
   `_DENIAL_NEXT_ACTIONS` for `at_capacity`, for every caller — unlike the ADR-0245 funding
   remedies (`accounting.set_quota`/`accounting.set_budget`), `resources.availability` needs no
   elevated role, so it is not gated on `caller_is_admin`.
4. **Typing stays mixed by design.** The pre-existing `cap`/`in_use` fields stay
   stringified (`denial_details`, unchanged, ADR-0123) for envelope compatibility; the three new
   fields are plain `int`/`dict[str, int]` because nothing existing reads them as strings. A
   consumer that sums `in_use_own_projects + in_use_other_projects` and compares it to `in_use`
   must cast one side.

Every other denial reason (`affinity_denied`, `budget_exceeded`, `quota_exceeded`, generic) is
unaffected: ADR-0132 decision 2 and ADR-0245's Decision continue to govern them unchanged.

## Consequences

- An agent reading only the envelope learns that `in_use` is host-wide, how much of it is its own
  vs. every other project's (never which), the same total by occupying state, and which tool
  (`resources.availability`) shows the rest of the fleet.
- `resources.availability`'s existing fleet-wide, project-affinity-filtered view (ADR-0070) is now
  a named recourse from the denial, not just a tool an agent might discover independently.
- No schema, migration, or persistence change; `allocations.request` advertises the generic
  denial envelope `outputSchema`, so the new `details` keys invalidate no committed snapshot.
- A future reason that wants the same breadcrumb can reuse `_HOST_CAP_NEXT_ACTIONS` by name; this
  ADR governs only `at_capacity`.

## Considered & rejected

- **Stringify the three new fields to match `cap`/`in_use`.** Rejected: nothing existing reads
  `AdmissionOutcome.details` values as strings (unlike the two fixed top-level fields, which
  `denial_details` stringifies itself), and an aggregate breakdown is more naturally `int`. The
  mixed typing is recorded here instead of hidden.
- **Gate `resources.availability` on caller role, like the ADR-0245 funding remedies.** Rejected:
  `resources.availability` requires no elevated role and every `allocations.request` caller
  (`Role.CONTRIBUTOR` or above) may already call it; gating it would withhold a diagnostic from
  exactly the caller who hit the denial.
- **Also add queue-wait-time guidance to the breadcrumb.** Out of scope for #2687 (explicit
  exclusion): queue ETA / wait-time estimation is a separate, larger feature; the existing
  `on_capacity="queue"` documentation already covers the recourse this denial can offer today.
