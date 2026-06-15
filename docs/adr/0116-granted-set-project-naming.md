# ADR 0116 — Name authorized projects in the granted-set accounting report (#426)

- **Status:** Proposed
- **Date:** 2026-06-15
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0043](0043-platform-scoped-rbac-tier.md)
  (the two accounting report forms and the read-shape audit rule — unchanged here).
- **Issue:** [#426](https://github.com/randomparity/kdive/issues/426).
- **Spec:** [`../design/granted-set-project-naming.md`](../design/granted-set-project-naming.md).

## Context

`accounting.report_granted_set` rolls up the caller's authorized projects. The
response builds `items` from the accounting domain's `report()`, which emits a
`RollupRow` only for a project that has at least one ledger row
(`src/kdive/services/accounting/ledger.py:130`). The cross-project `total` row
carries `project="*"` (ADR-0043 §3).

The consequence, surfaced by the first agent-driven run: a token granted exactly one
project with no spend yet receives `project_count: 1`, an **empty `items`** list, and
`total_project: "*"` — the granted project is never named. The agent could not learn
which project it could touch except by trial. (The demo token holds
`roles: {"demo": "admin"}`, so `demo` was in the resolved set; it was absent from
`items` purely because it had no ledger rows — confirmed by
`tests/security/authz/test_demo_oidc_claims.py`.)

The issue also asks us to decide whether *role-less* membership (a project in
`ctx.projects` with no `roles` entry, i.e. below the viewer floor) should appear in
the granted set. Today `_resolve_granted_set` drops it.

## Decision

We will **name every authorized granted-set project in the response, zero-filling
those with no ledger rows** in the selected window. `accounting.report_granted_set`
emits one item per *resolved target project*: a project with spend keeps its sums; a
project with none appears as a zero row (`reserved/reconciled/variance = 0`,
`principal` empty). The cross-project `total` row is unchanged (`project="*"`).

We will **keep the viewer floor unchanged**: a role-less member has no accounting-read
access to that project, so the default set continues to exclude it and the named path
continues to raise `AuthorizationError`. The honest "you are a member but hold no
role" surface is the sibling whoami tool ([#427](https://github.com/randomparity/kdive/issues/427)),
not this usage report.

The zero-fill applies to the granted-set form only; `accounting.report_all_projects`
keeps its documented universe-and-`project_count` contract (a budgeted-but-unspent
project is *considered*, not rowed).

## Consequences

- An agent can read the projects its token grants directly off
  `report_granted_set` items, without trial-and-error and without a usage history.
- The zero-fill is constructed in the tool layer from the resolved target set and a
  new domain helper `empty_row(project)` that uses `quantize_kcu`, so a zero-filled
  row serializes `"0.0000"` byte-identically to a real zero row (no `"0"` vs
  `"0.0000"` skew). Zero-filled projects are sorted by name and appended after the
  domain's `rollup.rows`, so item order is deterministic. The audit trigger still
  counts the *authorized set*, so audit behaviour is unchanged.
- The two report forms now shape "a project with no rows" differently (granted-set
  names it; all-projects does not). That asymmetry is intentional — own-project
  discovery vs cross-tenant oversight — and is documented in both the spec and the
  `accounting.report_all_projects` docstring.
- No schema, migration, auth, or dependency change. The advertised tool `outputSchema`
  is the flat `{"type": "object"}` (ADR-0113), so the committed tool reference is
  unaffected by the row-shape change.

## Alternatives considered

- **Lower the floor for the granted-set report so role-less members see their own
  project's accounting.** Rejected: it would make accounting the only project read
  that admits a role-less member, contradicting the system-wide viewer floor
  (`allocations.list`, `investigations.open`, the named granted-set path all deny
  role-less). Discovery of role-less membership belongs in whoami (#427), not in a
  usage report the member cannot otherwise read.
- **Also zero-fill `accounting.report_all_projects`.** Rejected as out of scope:
  it re-shapes an unrelated cross-tenant oversight report whose
  "considered-but-no-row" contract is already documented, and #426 is about
  own-project discovery. A uniform change can be made later under its own issue if
  oversight wants every budgeted project named.
- **Add a separate `total_projects` list field naming the set.** Rejected: it
  duplicates the item ids, leaves the empty-`items` confusion in place for clients
  that read `items`, and adds a field for one caller's benefit. Naming projects as
  items is the existing shape; zero-fill reuses it.
