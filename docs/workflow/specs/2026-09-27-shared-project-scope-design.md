# Shared list project scoping (#2714)

## Problem

Four list handlers duplicate readable-project selection; three also duplicate
optional-project narrowing. A policy edit can miss a copy.

## Scope

Move the existing bodies into `mcp/tools/_common.py` as `viewer_projects` and
`project_filter`. Systems, runs, and allocations import `project_filter` under
their existing private alias; debug sessions import `viewer_projects` likewise.
Remove their local definitions. Preserve membership order, role checks,
empty-result behavior, and debug-session SQL narrowing. This relocation into the
existing shared helper owner requires no compatibility wrapper or new layer.
The issue specifies this approach; keeping copies fails its single-owner criterion.
No API, database, or authorization policy changes. Excluded: debug SQL rewrite
(debug-session owner), jobs role helper and unrelated list APIs (their owners).

### Failure model

- Actors and deployments: authenticated MCP tenants in supported service deployments.
- Invariants and assets: visible projects remain membership intersected with granted
  roles; an optional project narrows that set and never reveals another tenant.
- Accepted failure classes: none introduced by this relocation.
- Covered elsewhere: authentication and role validity belong to authz middleware;
  debug SQL and cursor validation retain existing handlers and regression tests.

## Success

The four named handlers share one readable-project implementation. The three
optional-project helpers share one implementation. Existing list tests, including
unreadable-project cases, pass unchanged; debug SQL remains byte-for-byte intact.

## Validation

- task-test-not-applicable: pure relocation and import migration introduce no new
  behavior or structural boundary beyond the requested consolidation; inspect the
  diff for one owner and removed copies rather than adding implementation-mirroring tests.
- Existing behavior proof: run `tests/mcp/lifecycle/test_systems_list.py`,
  `tests/mcp/lifecycle/test_runs_list.py`,
  `tests/mcp/lifecycle/test_allocations_tools.py`, and `tests/mcp/debug/test_debug_session_read.py`.
- Run `just lint`, `just type`, relevant doc checks, and installed pre-push `just ci`.
  Review tenancy narrowing and unchanged SQL independently before delivery.
