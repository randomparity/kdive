# ADR 0261 — Role-filter success-envelope `suggested_next_actions` (#862)

- **Status:** Accepted
- **Date:** 2026-06-26
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0148](0148-rbac-scoped-tool-exposure.md) (the
  connection-scoped `list_tools` exposure filter and the reviewed `_TOOL_SCOPES` classification
  this reuses), [ADR-0245](0245-alloc-denial-remedy-breadcrumbs.md) /
  [ADR-0255](0255-aggregate-funding-gate-denials.md) (the role-aware **denial**-path breadcrumb
  precedent this generalizes), [ADR-0006](0006-oidc-rbac-attribution.md) /
  [ADR-0020](0020-rbac-audit-gate-implementation.md) (project-scoped RBAC and `require_role`).
- **Issue:** [#862](https://github.com/randomparity/kdive/issues/862).
- **Spec:** [`../superpowers/specs/2026-06-26-role-filter-success-next-actions-862.md`](../superpowers/specs/2026-06-26-role-filter-success-next-actions-862.md).

## Context

Every tool returns a uniform envelope whose `suggested_next_actions` lists the literal next-tool
breadcrumbs an agent should follow. On the **success** path these are emitted unfiltered by the
caller's role. A granted `allocations.request` — a `Role.CONTRIBUTOR` tool — returns
`["allocations.get", "systems.provision", "allocations.release"]`, but `systems.provision` is
`Role.OPERATOR`-only (`mcp/exposure.py` `_TOOL_SCOPES`; handler `required_role=Role.OPERATOR`). A
plain contributor that follows the breadcrumb hits a `RoleDenied`.

The **denial** path already filters by role (ADR-0245/0255): `_denial_next_actions` leads with
the admin remedy tools only for an admin caller. The mechanism — check the caller's role on the
allocation's project, then prune actions they cannot invoke — exists; it just never ran on the
success path.

`ToolExposureMiddleware` (ADR-0148) is not a substitute. It filters `list_tools()` only, never
envelope suggestions, and it is **connection-scoped**: it admits a tool if the caller could
invoke it under *some* project grant. An allocation is single-project, so the connection union
would still advertise `systems.provision` to a caller who is operator on another project but only
contributor on the allocation's project.

## Decision

Filter every allocation success-envelope `suggested_next_actions` against the caller's grant on
the **allocation's project** before emitting, reusing the reviewed `_TOOL_SCOPES` classification
via a new **project-scoped** visibility helper.

1. **Two new helpers in `mcp/exposure.py`**, beside the connection-scoped `tool_visible`:
   - `project_tool_visible(tool_name, ctx, project)` — a project-role scope is satisfied only by
     the role held on `project` itself (`ctx.roles.get(project)`), not the connection-wide max
     `_max_project_rank`; a platform-role scope (not project-scoped) reuses `_has_platform`.
     Public tools (empty scope set) stay always-visible.
   - `visible_next_actions(actions, ctx, project)` — keep only invokable actions, preserving
     order, no dedup.
2. **`allocation_next_actions(state)` stays a pure candidate list.** Filtering is composed at the
   emit site, so breadcrumb *generation* (state machine) stays separate from breadcrumb
   *authorization* (RBAC). The four allocation success sites — grant/enqueue
   (`request.py`), `envelope_for_allocation` (`get`/`wait`/`list`), `renew` (`lifecycle.py`),
   and the `list` collection breadcrumb (`view.py`) — wrap their candidates in
   `visible_next_actions(..., ctx, alloc.project)`. `_grant_or_enqueue_response`,
   `envelope_for_allocation`, and `_renew_response` gain a `ctx` parameter; their callers already
   hold one.
3. **Project-scoped, matching the denial precedent.** The denial path keys off
   `result.project in projects_with_role(ctx, Role.ADMIN)`; the success filter keys off the same
   per-project role for parity.

This is a presentation change only: no envelope schema, error category, RBAC enforcement, tool
surface, or migration change. Execution-time `require_role` remains the authorization boundary;
the filter only stops advertising a breadcrumb the caller cannot reach.

## Consequences

- A contributor's granted allocation no longer points at `systems.provision`; the breadcrumb
  collapses to `["allocations.get", "allocations.release"]`. The filter is uniform, so a viewer
  reading the same allocation also drops the contributor `allocations.release`.
- The fix covers `get` / `wait` / `list` / `renew`, not just `request`, because all share the
  `GRANTED` breadcrumb.
- An envelope may now carry `suggested_next_actions: []` (already permitted by the contract). An
  agent that relied on a fixed-length breadcrumb must read the list, not an index.
- The project-scoped helper is reusable; other planes' success envelopes can adopt it without
  re-deriving the scope check. This change does not wire them (out of scope).
- No new false negatives: the filter only ever removes an action whose `_TOOL_SCOPES`
  classification the caller fails on `project`; the classification is `<=` each handler's real
  `require_role`, and the completeness guard keeps it total.

## Considered & rejected

- **Reuse the connection-scoped `tool_visible` / middleware union.** Rejected: it would keep
  advertising an operation the caller can perform on *another* project but not on this
  allocation's project. An allocation is single-project; the suggestion must be too.
- **Substitute a "contributor provisioning path" (`systems.define` → upload →
  `systems.provision_defined`) for non-operators.** Rejected: `systems.define` and
  `systems.provision_defined` are *also* `_OPERATOR` in `_TOOL_SCOPES`, so the substitute is
  equally unreachable for a contributor. Omitting the unreachable action is the only correct
  reduction; the issue text's substitute suggestion does not hold against the current
  classification.
- **Push the filter into `allocation_next_actions(state, ctx, project)`.** Rejected: it couples
  the state-machine breadcrumb generator to a `RequestContext` and forces every (including
  failure-path) caller to thread authz. Composing a separate `visible_next_actions` step keeps
  each function single-purpose and testable in isolation.
- **A hardcoded "drop `systems.provision` for non-operators" special-case.** Rejected: it does
  not generalize, re-implements the classification already in `_TOOL_SCOPES`, and silently rots
  if a breadcrumb gains another gated tool. Reusing the reviewed scope map is self-maintaining.
- **Post-filter the envelope after construction (rewrite `response.suggested_next_actions`).**
  Rejected: it mutates a constructed envelope and scatters the authz decision away from the emit
  site; composing the filtered list before construction is clearer.
