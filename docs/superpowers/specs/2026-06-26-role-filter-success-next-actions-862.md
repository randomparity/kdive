# Role-filter success-envelope `suggested_next_actions` (#862)

- **Issue:** [#862](https://github.com/randomparity/kdive/issues/862)
- **ADR:** [ADR-0261](../../adr/0261-role-filter-success-next-actions.md)
- **Status:** Draft
- **Date:** 2026-06-26

## Problem

`suggested_next_actions` on **success** envelopes are emitted unfiltered by the caller's
role. A granted `allocations.request` (a `Role.CONTRIBUTOR` tool) returns the breadcrumb
`["allocations.get", "systems.provision", "allocations.release"]`, but `systems.provision`
is `Role.OPERATOR`-only (`mcp/exposure.py` `_TOOL_SCOPES["systems.provision"] == _OPERATOR`;
handler `required_role=Role.OPERATOR`). A plain contributor that follows the breadcrumb
walks straight into a `RoleDenied` authorization error.

The only existing role filtering is on the **funding-denial** path
(`allocations/request.py` `_denial_next_actions(..., caller_is_admin=...)`, ADR-0245/0255):
it leads with `accounting.set_quota` / `accounting.set_budget` only for an admin caller. The
success path has no equivalent. `ToolExposureMiddleware` (ADR-0148) filters `list_tools()`
only — it never touches envelope suggestions, and it is connection-scoped (a union across all
the caller's projects), so it cannot answer "can this caller provision *this* allocation's
project".

## Scope

The allocation success-envelope emit sites and one shared helper. Out of scope: `runs/`,
`artifacts/`, and other planes' envelopes (owned elsewhere); the helper is written reusably so
those can adopt it later, but this change only wires the allocation path.

## Affected emit sites (all in `mcp/tools/lifecycle/allocations/`)

The leak is anywhere a success envelope can carry an action above the caller's project role.
`allocation_next_actions(AllocationState.GRANTED)` is the only breadcrumb that lists
`systems.provision`, but the same filter is applied uniformly so a `viewer` is never pointed at
the `contributor` `allocations.release` either:

1. `request.py` `_grant_or_enqueue_response` — grant/enqueue path (GRANTED → `systems.provision`).
2. `common.py` `envelope_for_allocation` — used by `allocations.get` / `allocations.wait` /
   `allocations.list` (`view.py`); a `viewer` reading a GRANTED allocation hits the same leak.
3. `lifecycle.py` `_renew_response` — `allocations.renew` of a GRANTED allocation stays GRANTED
   and re-emits `systems.provision`.
4. `view.py` `list_allocations` collection-level `suggested_next_actions`
   (`["allocations.get", "allocations.release"]`).

## Decision (summary; full rationale in ADR-0261)

Filter every allocation success-envelope `suggested_next_actions` against the caller's grant on
the **allocation's project** before emitting, using a new project-scoped visibility helper that
reuses the reviewed `_TOOL_SCOPES` classification.

### New shared helpers (`mcp/exposure.py`)

- `project_tool_visible(tool_name, ctx, project) -> bool` — project-scoped counterpart to the
  existing connection-scoped `tool_visible`. A **project**-role scope is satisfied only by the
  role held on `project` itself (`ctx.roles.get(project)`), not the connection-wide max; a
  **platform**-role scope (not project-scoped) uses the connection's platform grants. Public
  tools (empty scope set) are always visible.
- `visible_next_actions(actions, ctx, project) -> list[str]` — return only the `actions` the
  caller could invoke for `project`, preserving order and not deduplicating.

`allocation_next_actions(state)` stays a pure candidate-list function; filtering is a separate
composition step applied at each emit site (separation of breadcrumb generation from authz).

### Wiring

Each of the four sites wraps its breadcrumb in
`visible_next_actions(<candidates>, ctx, <alloc.project>)`. `_grant_or_enqueue_response`,
`envelope_for_allocation`, and `_renew_response` take a `ctx: RequestContext` parameter (their
callers already hold one); `envelope_for_allocation` derives `project` from `alloc.project`.

## Why project-scoped, not connection-scoped

An allocation belongs to one project. A caller may be `operator` on project B and `contributor`
on project A; an allocation on A must not suggest `systems.provision` just because the caller is
an operator somewhere else. This matches the denial-path precedent
(`result.project in projects_with_role(ctx, Role.ADMIN)`), which is per-project.

## Behavioural contract / acceptance criteria

For a GRANTED allocation on project `P`:

- caller holds `Role.OPERATOR`+ on `P` → `["allocations.get", "systems.provision", "allocations.release"]`
  (unchanged).
- caller holds `Role.CONTRIBUTOR` on `P` → `["allocations.get", "allocations.release"]`
  (`systems.provision` dropped).
- caller holds `Role.VIEWER` on `P` → `["allocations.get"]` (`systems.provision` and the
  contributor `allocations.release` dropped).
- caller is a member of `P` with no role → `["allocations.get"]` is dropped too (member without a
  role satisfies no scope; `allocations.get` is `viewer`), leaving `[]`.
- caller holds `Role.OPERATOR` on a *different* project, `Role.CONTRIBUTOR` on `P` →
  `systems.provision` dropped (project-scoped).

The `allocations.list` collection-level breadcrumb (`["allocations.get", "allocations.release"]`,
scoped to the list's `project`) filters the same way: a `contributor` listing keeps both, a
`viewer` listing keeps `["allocations.get"]`.

The same filter applies on `get` / `wait` / `list` / `renew`. No envelope schema, error
category, RBAC enforcement, tool surface, or migration changes — execution-time `require_role`
remains the boundary; this only stops advertising an unreachable breadcrumb.

## Failure modes / edges

- **Empty result after filtering** is valid: an envelope may carry `suggested_next_actions: []`.
  `ToolResponse.success` stores `suggested_next_actions or []` and the model validator constrains
  only `error_category`, so an empty success breadcrumb is permitted at construction.
- **Unknown tool name** in a breadcrumb: `required_scopes` returns the empty set (public
  default) → kept. Not a concern here (all breadcrumbs are real classified tools); the
  completeness guard (`tests/mcp/core/test_app.py`) keeps that classification honest.
- **Member, no role**: filtered to public-only actions; never raises.

## Testing

Behavioural tests at the handler boundary (inject `RequestContext`), covering grant/get/renew
across viewer / contributor / operator / admin / no-role / operator-elsewhere, asserting the
exact filtered breadcrumb. Unit tests for `project_tool_visible` / `visible_next_actions` on the
project-vs-connection distinction (operator-elsewhere) and order preservation.
