# ADR 0245 — Name the remedy tool on quota and budget allocation denials (#801)

- **Status:** Accepted
- **Date:** 2026-06-25
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0174](0174-config-error-detail.md) (actionable
  denial `detail`/`reason`), the #471 discovery-action denial breadcrumbs
  (`mcp/tools/lifecycle/allocations/request.py`).
- **Issue:** [#801](https://github.com/randomparity/kdive/issues/801) (part of epic #800).
- **Spec:** [`../superpowers/specs/2026-06-25-alloc-denial-remedy-801.md`](../superpowers/specs/2026-06-25-alloc-denial-remedy-801.md).

## Context

A fresh project's first `allocations.request` fails closed twice in sequence: first
`quota_exceeded` (admission's `_within_alloc_quota` reads a missing quota row as `False`),
then — after `accounting.set_quota` — `budget_exceeded` (`within_budget` reads a missing
budget row as `False`). The fail-closed behaviour is correct, but the denial envelope is
undiscoverable: `_denial_response` hard-codes `suggested_next_actions = ["allocations.list"]`
for every category and `_denial_detail` names no remedy. On a denied first request
`allocations.list` returns an empty list, so the agent learns nothing. The Part 4 black-box
review marked this the only defect that genuinely blocked progress.

The two admin tools that resolve these denials already exist and are registered:
`accounting.set_quota` and `accounting.set_budget` (`mcp/exposure.py`).

## Decision

We will branch the denial envelope on category in
`mcp/tools/lifecycle/allocations/request.py`:

- A `QUOTA_EXCEEDED` denial sets `suggested_next_actions = ["accounting.set_quota",
  "allocations.list"]` and a `detail` that names `accounting.set_quota`.
- A budget denial (`reason == BUDGET_DENIAL_REASON`) sets `["accounting.set_budget",
  "allocations.list"]` and a `detail` that names `accounting.set_budget`.
- Every other category (host-capacity `at_capacity`, affinity, generic) keeps the existing
  `["allocations.list"]` breadcrumb and detail unchanged.

The remedy breadcrumb is advisory and is **not** gated on the caller's role, matching the
existing denial-breadcrumb contract (denials already list next-step tools without
re-checking authz).

## Consequences

- An agent (or its operator) hitting a quota or budget denial is told the exact accounting
  tool that resolves it, in both the machine-readable `suggested_next_actions` and the
  human `detail`. The two-step trap remains two steps, but each step now self-describes its
  fix.
- `accounting.set_quota` / `accounting.set_budget` are admin-scoped, so a non-admin
  contributor cannot call the named tool themselves. The breadcrumb stays diagnostic — it
  tells them precisely what an operator must do, which is strictly more actionable than the
  empty `allocations.list` it replaces. We accept naming an admin tool to a non-admin caller
  rather than rendering role-conditional breadcrumbs (see rejected alternatives).
- No change to the admission gate, the accounting tools, RBAC, request/response schemas, or
  migrations. `allocations.request` advertises the generic envelope outputSchema, so the new
  breadcrumb values invalidate no committed snapshot.

## Alternatives considered

- **Collapse the two denials into one "project not yet funded; set quota + budget" denial.**
  Rejected here as **needs-design** (tracked separately): it requires distinguishing "no
  row" from "value is 0" in `_within_alloc_quota` / `within_budget`, which today both
  collapse to `False`. The issue explicitly scopes that out.
- **Gate the breadcrumb on the caller's role** — show the remedy tool only to admins, show
  `allocations.list` to everyone else. Rejected: the breadcrumb is advisory, the denial path
  has the caller's role but the existing contract never role-filters next-step suggestions,
  and hiding the remedy from a non-admin removes the one piece of information that tells them
  what to ask an operator for. Diagnostic value outweighs the minor "tool you can't call"
  wrinkle.
- **Auto-seed quota/budget rows for admin-created projects.** Rejected — out of scope and a
  larger behavioural change to project provisioning (`admin/bootstrap.py`); it would mask the
  fail-closed default rather than make the denial discoverable, and changes who owns funding
  decisions.
