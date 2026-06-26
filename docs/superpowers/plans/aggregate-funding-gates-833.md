# Implementation plan — Aggregate quota + budget admission denials (#833)

- **Spec:** [`../specs/2026-06-26-aggregate-funding-gates-833.md`](../specs/2026-06-26-aggregate-funding-gates-833.md)
- **ADR:** [ADR-0255](../../adr/0255-aggregate-funding-gate-denials.md)
- **Branch:** `feat/aggregate-funding-gates-833`

## Conventions & guardrails (apply to every task)

- Python 3.14, `uv`. TDD: write the failing test first, confirm it fails for the right reason,
  then the minimal implementation.
- Per-commit guardrails: `just lint` (ruff check + format), `just type` (ty, **whole tree**),
  and the focused tests for the touched module. Before pushing: full `just ci`.
- Limits: ≤100 lines/function, complexity ≤8, ≤5 positional params, 100-char lines, absolute
  imports only, Google-style docstrings on non-trivial public APIs. Zero warnings.
- No migration (read-path only). Conventional-commit subjects ≤72 chars, `Co-Authored-By`
  trailer required.
- Real Postgres for admission tests via the `migrated_url` fixture (see
  `tests/services/test_admission_budget_quota.py`); transport tests via the `_pool` fixture in
  `tests/mcp/lifecycle/test_allocations_tools.py`.

## Task 1 — Service: `quota_status` + `funding_unmet` helpers (no behaviour change yet)

**Where it fits:** the service-layer source of the aggregated `unmet` list. Pure read helpers;
nothing wires them to a denial yet, so the gate's routing stays identical.

**Files:** `src/kdive/services/allocation/admission/core.py` (add helpers; reuse
`OCCUPYING_VALUES` and `budget_snapshot`). Tests in
`tests/services/test_admission_budget_quota.py`.

**Steps (TDD):**

1. Add `async def quota_status(conn, project) -> tuple[int | None, int]` returning
   `(max_concurrent_allocations | None, occupying_count)`. `None` limit ⇔ no quota row.
   Occupying count = `count(*)` over `OCCUPYING_VALUES` for the project (same query
   `_within_alloc_quota` uses). Factor the shared count so the two cannot drift, or keep one
   query and have `_within_alloc_quota` derive its bool from `quota_status` — preferred:
   reimplement `_within_alloc_quota` as `limit is not None and count < limit` over
   `quota_status`, so the gate predicate and the report read identical figures.
2. Add `async def funding_unmet(conn, project, estimate: Decimal) -> list[dict[str, Any]]`:
   - quota entry when `limit is None or count >= limit`:
     `{"gate": "quota", "current": count, "required": count + 1}` and `"limit": limit` when
     `limit is not None`.
   - budget entry when over budget: read `budget_snapshot`; unmet iff `snapshot is None` or
     `limit_kcu - spent_kcu < estimate`. Entry:
     `{"gate": "budget", "required_kcu": str(estimate)}`, plus when a row exists:
     `"required_limit_kcu": str(spent_kcu + estimate)`, `"limit_kcu"`, `"spent_kcu"`,
     `"remaining_kcu": str(limit_kcu - spent_kcu)`. When no row: only `required_kcu` (and set
     `required_limit_kcu = str(estimate)` too, since spent is unknown/0 — keep symmetric and
     non-misleading; document that absent-row budget reports the estimate as the limit to set).
   - Order: quota first, then budget. Return `[]` when neither is unmet.

**Acceptance:** unit tests over a seeded DB: (a) `quota_status` returns `(None, 0)` with no
row, `(cap, n)` with a row + n occupying; (b) `funding_unmet` returns `[quota,budget]`,
`[quota]`, `[budget]`, `[]` for the four states; (c) budget entry's `required_limit_kcu ==
spent + estimate` when a row exists, and figures omitted when no row; (d) quota `required ==
current + 1`.

**Rollback:** pure additions; deleting the two functions + their tests reverts cleanly.

## Task 2 — Gate: drop the inline `_budget_denial_details`; budget denial returns bare

**Where it fits:** removes the #838 flat keys from the gate so enrichment is the single source
of figures. The gate's category/reason/queueable are unchanged.

**Files:** `core.py` (`admission_gate`, delete `_budget_denial_details`).

**Steps (TDD):**

1. Update `tests/services/test_admission_budget_quota.py::test_budget_denial_details_carry_estimate_and_remaining`
   and `…omit_figures_without_budget_row` to the new contract **in Task 3** (they assert the
   pre-enrichment shape; they will move to assert `outcome.details["unmet"]`). For Task 2,
   temporarily assert the budget denial still has category/reason set and `details == {}` —
   then Task 3 supersedes.
   - Simpler: fold Tasks 2 + 3 into one commit so the tests never assert an intermediate
     contract. **Decision: implement Tasks 2 and 3 together** (the bare denial is only correct
     once enrichment exists). Keep them as one logical commit.
2. In `admission_gate`, the budget branch returns `AdmissionOutcome(... reason=BUDGET_DENIAL_REASON)`
   with **no** `details` (drop the `_budget_denial_details` call). Delete the function.

**Acceptance:** see Task 3 (combined).

## Task 3 — Synchronous enrichment: attach `details["unmet"]` to funding denials

**Where it fits:** the core behaviour. Only the synchronous `admit` path enriches; the
promotion sweep replays `admission_gate` directly and is untouched.

**Files:** `core.py` (`_admit_under_project_lock` / `_deny_or_enqueue`). Add `_is_funding_denial`
predicate and an `_enrich_funding_denial` step.

**Steps (TDD):**

1. Failing tests in `test_admission_budget_quota.py`:
   - both unmet (no quota row, no budget row): `outcome.category is QUOTA_EXCEEDED`,
     `outcome.details["unmet"]` has gates `["quota","budget"]` with the documented figures.
   - quota-only (no quota row, generous budget): `unmet == [quota]`.
   - budget-only (generous quota, short budget): `outcome.reason == BUDGET_DENIAL_REASON`,
     `unmet == [budget]` with `required_limit_kcu == spent + estimate`.
   - host-cap denial: `"unmet" not in outcome.details` (cap=1, second request).
   - affinity denial: no `unmet` (scoped resource, foreign project) — reuse existing affinity
     test scaffolding if present, else assert via a host-cap/affinity path that no unmet key.
   - all-or-nothing preserved: still `count(allocations)==0`, `count(ledger)==0`, `spent==0`
     on each denial.
2. `_is_funding_denial(denial)`: `category is QUOTA_EXCEEDED` or
   `(category is ALLOCATION_DENIED and reason == BUDGET_DENIAL_REASON)`.
3. Thread `estimate` into `_deny_or_enqueue`. In its return-the-denial branch (i.e. not
   enqueueing): if `_is_funding_denial(denial)`, compute `unmet = await funding_unmet(conn,
   project, estimate)` and return `dataclasses.replace(denial, details={**denial.details,
   "unmet": unmet})`; else return the denial unchanged. The enqueue branch is unchanged.
4. Confirm the enrichment runs **inside** the open PROJECT-locked transaction (it is — called
   from `_admit_under_project_lock`).

**Acceptance:** the Task-1 + Task-3 tests pass; `test_admission_budget_quota.py` updated #838
tests now assert `unmet` instead of flat keys; promotion tests untouched and green; no new
write on any denial.

**Rollback:** revert `_deny_or_enqueue` to not enrich and restore `_budget_denial_details`.

## Task 4 — Transport: surface `unmet` remedy, next-actions, and prose

**Where it fits:** turns the service `unmet` list into the agent-facing envelope; owns the MCP
tool names (ADR-0245).

**Files:** `src/kdive/mcp/tools/lifecycle/allocations/request.py`. Tests in
`tests/mcp/lifecycle/test_allocations_tools.py`.

**Steps (TDD):**

1. Failing transport tests (via `_pool`/`_register`/`_request`, admin and non-admin ctx):
   - both unmet, admin: `error_category == "quota_exceeded"`; `data["unmet"]` has two entries
     each with a `remedy` (`accounting.set_quota`, `accounting.set_budget`);
     `suggested_next_actions == ["accounting.set_quota", "accounting.set_budget",
     "allocations.list"]`; `detail` names both shortfalls and both tools.
   - both unmet, non-admin: `suggested_next_actions == ["allocations.list"]`; `detail` names
     both shortfalls + "ask your project admin"; no `accounting.` tool in `detail`.
   - budget-only admin/non-admin: parity with the existing #838/#841 tests, rewritten to read
     `data["unmet"][0]` figures (`required_kcu`/`required_limit_kcu`/`remaining_kcu`) instead
     of the removed flat keys; the shortfall figure still appears in `detail`.
   - quota-only: `data["unmet"] == [quota]` with remedy; admin next-actions lead `set_quota`.
   - host-cap / affinity: `"unmet" not in data`; envelope unchanged from today.
2. Add a gate→remedy map `{"quota": _QUOTA_REMEDY_TOOL, "budget": _BUDGET_REMEDY_TOOL}`.
   In `_denial_response`, after `data = denial_details(outcome)`, if `data` has `unmet`, inject
   `entry["remedy"]` per gate (skip unknown gates defensively).
3. Rewrite `_denial_next_actions`: for an admin caller with `unmet`, return
   `[*ordered remedies, *_DENIAL_NEXT_ACTIONS]`; non-admin → `_DENIAL_NEXT_ACTIONS`; no
   `unmet` (host-cap/affinity) → existing behaviour.
4. Rewrite `_denial_detail` / replace `_budget_denial_detail`: when `unmet` present, compose a
   role-aware sentence enumerating each gate's shortfall + remedy (quota: "concurrency quota
   exhausted (in use {current}/{limit})"; budget: "budget exhausted (requested {required_kcu}
   kcu, {remaining_kcu} kcu remaining)"; omit the parenthetical figure clause when its source
   field is absent). Keep affinity / at_capacity / generic branches as today. Preserve the
   role-aware remedy clauses (`_quota_remedy_clause`/`_budget_remedy_clause`).
5. Update the direct unit tests of `_budget_denial_detail`/`_denial_next_actions`/`_denial_detail`
   to the new functions/signatures and `unmet`-sourced inputs.

**Acceptance:** all transport tests green; the `unmet` array is JSON-valid in `data` (verified
by `ToolResponse` construction not raising); role-awareness preserved.

**Rollback:** revert `request.py` to the category-branched detail/next-actions and restore the
flat-key reads.

## Task 5 — Full guardrails + branch review

1. `just ci` (lint, type whole-tree, lint-shell, lint-workflows, check-mermaid, test) green.
2. Grep for any remaining reader of the removed flat keys in denial context
   (`estimate_kcu`/`budget_remaining_kcu`/`limit_kcu`/`spent_kcu` in
   `allocations/request.py` + admission tests); the `accounting.estimate` tool's own
   `estimate_kcu` is unrelated and must stay.
3. `/challenge --base main` review loop; address findings.
4. Open the PR; drive to green + mergeable.

## Verification matrix (maps to spec success criteria)

| spec criterion | covered by |
|----------------|------------|
| 1 both-unmet aggregate | Task 3 service test + Task 4 transport test (admin) |
| 2 single-gate lists one | Task 3 quota-only / budget-only tests |
| 3 host-cap/affinity/PCIe no unmet | Task 3 + Task 4 host-cap/affinity tests |
| 4 promotion routing + all-or-nothing unchanged | existing promotion tests stay green; Task 3 no-write asserts |
| 5 non-admin both-unmet | Task 4 non-admin test |
| 6 no migration, ci green | Task 5 |
