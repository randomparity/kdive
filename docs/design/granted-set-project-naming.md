# Granted-set report: name authorized projects, affirm the viewer floor

- **Issue:** [#426](https://github.com/randomparity/kdive/issues/426)
- **ADR:** [`../adr/0116-granted-set-project-naming.md`](../adr/0116-granted-set-project-naming.md)
- **Status:** Draft

## Problem

During the first full agent-driven MCP run, an agent had no reliable way to learn
which project its token grants. `accounting.report_granted_set` returned
`project_count: 1` but with an **empty `items`** list and `total_project: "*"` — it
never named the granted project. The agent found `demo` only by trial-and-error.

Two distinct facts produce that surface:

1. **The granted project is never named when it has no ledger rows.**
   `accounting.report_granted_set` resolves the caller's member-with-role projects
   (the demo token holds `roles: {"demo": "admin"}`, so `demo` *is* in the resolved
   set — confirmed by `tests/security/authz/test_demo_oidc_claims.py`). But
   `_report_response` builds `items` only from `rollup.rows`, and the accounting
   domain's `report()` emits a row only for a project that has ≥1 ledger row
   (`src/kdive/services/accounting/ledger.py:130`). A granted project with no spend
   yet contributes nothing, so the only project identifier left in the envelope is
   the cross-project total row's `total_project: "*"`. **This is the observed bug.**

2. **Role-less membership is dropped from the default granted set.**
   `_resolve_granted_set` keeps `[p for p in ctx.projects if ctx.roles.get(p) is not
   None]` (`reports.py:126`). A token carrying `projects: ["x"]` with no `roles`
   entry for `x` yields `ctx.roles.get("x") is None`, so `x` is excluded. The issue
   asks us to *decide* whether such viewer-floor-failing membership should appear.

## Decision summary

See ADR-0116 for the full record. In brief:

1. **Name every resolved granted-set project, zero-filling those with no ledger
   rows.** `accounting.report_granted_set` emits one item per *authorized target
   project*, not one per project-with-spend. A project with no ledger rows in the
   selected window appears as a zero row (`reserved/reconciled/variance = 0`,
   `principal` empty), so the caller can read off exactly which projects the token
   authorizes. The cross-project `total` row is unchanged (`total_project: "*"`).

2. **Keep the viewer floor; do not surface role-less membership in this report.**
   `require_role(ctx, project, VIEWER)` raises `RoleDenied` when the held role is
   `None` (`rbac.py:138-140`); every project read in the system (`allocations.list`,
   `investigations.open`, the named granted-set path) sits on that floor. A role-less
   member therefore has *no* accounting-read access to that project, and the default
   set correctly excludes it — consistent with the named path, which already raises
   for a role-less project. The honest discovery surface for "you are a member but
   hold no role" is the sibling `projects.list` whoami tool ([#427](https://github.com/randomparity/kdive/issues/427)),
   which projects full `ctx.projects` including role-less membership. This report
   stays a *usage* report over projects the caller may actually read.

## Scope boundary

Zero-fill naming applies to the **granted-set** form only. The `accounting.report_all_projects`
oversight form keeps its documented contract: its universe (`SELECT project FROM
ledger UNION SELECT project FROM budgets`) is reported in `project_count`, and a
budgeted-but-unspent project contributes no row (it was *considered*, not *named*).
That cross-tenant total is a different use case from own-project discovery and is out
of scope for #426; changing it would re-shape an unrelated report.

## Acceptance criteria

- `accounting.report_granted_set` over a single granted project with **zero** ledger
  rows returns `status == "ok"`, `project_count == "1"`, and exactly one item whose
  `project` is that project name with zero `reserved/reconciled/variance`.
- A granted set of two projects where only one has spend names **both** projects
  (the spent one with its sums, the other zero-filled).
- The cross-project `total` row is unchanged: `total_project == "*"` and the totals
  equal the sum over projects with spend.
- Audit behaviour is unchanged: the audit trigger still counts the *authorized set*
  (>1 project OR `group_by=principal`), independent of how many projects have rows.
- The default granted set still excludes role-less membership; the named path still
  raises `AuthorizationError` for a role-less or non-member project (no floor change).
- `accounting.report_all_projects` behaviour and committed tool reference are unchanged.
- A one-line docstring note records that `allocations.list` accepting a granted
  `project` is working-as-designed (the issue's "record only" item), so it is not
  later "fixed" as a non-bug.

## Out of scope

- The `projects.list` whoami tool (sibling issue #427).
- Any change to the viewer floor or to `roles_from_claims`.
- Any change to `accounting.report_all_projects` row shaping.
