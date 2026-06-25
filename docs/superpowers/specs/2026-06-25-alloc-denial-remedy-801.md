# Allocation-denial remedy breadcrumbs (#801)

- **Issue:** [#801](https://github.com/randomparity/kdive/issues/801) (Part 4 black-box review, epic #800).
- **ADR:** [ADR-0245](../../adr/0245-alloc-denial-remedy-breadcrumbs.md).
- **Status:** Accepted.

## Problem

On a fresh project the first `allocations.request` fails closed twice in sequence —
`quota_exceeded` (no quota row → `_within_alloc_quota` reads `None` as `False`), then,
after `accounting.set_quota`, `budget_exceeded` (no budget row → `within_budget` reads
`None`/`limit_kcu = 0` as `False`). The denial envelope is correct to fail closed, but it
points nowhere useful: `_denial_response` hard-codes `suggested_next_actions =
["allocations.list"]` for **every** category, and `_denial_detail` names no remedy tool.
`allocations.list` on a denied first request returns an empty list — zero signal.

The two admin tools that resolve these denials already exist and are registered:
`accounting.set_quota` and `accounting.set_budget` (`mcp/exposure.py`, both `_ADMIN`).

## Scope

In scope (`src/kdive/mcp/tools/lifecycle/allocations/request.py` only):

- A quota denial (`ErrorCategory.QUOTA_EXCEEDED`) names `accounting.set_quota` first in
  `suggested_next_actions` and in `detail`.
- A budget denial (`reason == BUDGET_DENIAL_REASON`) names `accounting.set_budget` first in
  `suggested_next_actions` and in `detail`.
- Every other denial category (host capacity `at_capacity`, affinity, generic) keeps the
  existing `["allocations.list"]` breadcrumb and detail unchanged.

Out of scope (explicitly):

- The "stretch" single-denial collapse that distinguishes "no row" from "value is 0" in
  `_within_alloc_quota` / `within_budget`. That is **needs-design** and tracked separately;
  the sequential two-step denial is preserved here.
- Any change to the admission gate, the accounting tools, RBAC, schemas, or migrations.

## Behaviour

| denial | category | `suggested_next_actions` | `detail` names |
| --- | --- | --- | --- |
| concurrency quota | `quota_exceeded` | `["accounting.set_quota", "allocations.list"]` | `accounting.set_quota` |
| budget | `allocation_denied` (`reason=budget_exceeded`) | `["accounting.set_budget", "allocations.list"]` | `accounting.set_budget` |
| host capacity | `allocation_denied` (`reason=at_capacity`) | `["allocations.list"]` | unchanged prose |
| affinity | `allocation_denied` (`reason=affinity_denied`) | `["allocations.list"]` | unchanged prose |

The remedy tools are admin-scoped; the breadcrumb is advisory. A non-admin contributor who
hits the denial still learns the exact tool an operator must run, which is strictly more
actionable than `allocations.list`. The breadcrumb is not gated on caller role (matching the
existing advisory-breadcrumb contract — denials already list tools without re-checking
authz).

## Acceptance

- A test asserts a quota denial carries `accounting.set_quota` in both
  `suggested_next_actions` and `detail`.
- A test asserts a budget denial carries `accounting.set_budget` in both
  `suggested_next_actions` and `detail`.
- A test asserts a host-capacity denial is unchanged (`["allocations.list"]`, no remedy tool
  in detail) — the branch does not regress the other categories.
