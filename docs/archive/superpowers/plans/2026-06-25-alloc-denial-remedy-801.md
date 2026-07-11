# Plan — Allocation-denial remedy breadcrumbs (#801)

Derived from [`../specs/2026-06-25-alloc-denial-remedy-801.md`](../specs/2026-06-25-alloc-denial-remedy-801.md)
and [ADR-0245](../../adr/0245-alloc-denial-remedy-breadcrumbs.md). Single tightly-coupled
change in one source file; implemented directly in-session (no subagent fan-out).

## Task 1 — branch the denial envelope on category

**Where it fits:** Closes #801. The denial render at
`src/kdive/mcp/tools/lifecycle/allocations/request.py` (`_denial_response` ~:167,
`_denial_detail` ~:180) hard-codes `["allocations.list"]` and names no remedy. Branch both on
the denial's reason/category so quota and budget denials point at the admin tool that resolves
them.

**Files touched:**
- `src/kdive/mcp/tools/lifecycle/allocations/request.py` — implementation.
- `tests/mcp/lifecycle/test_allocations_tools.py` — tests (handler-level, injected pool, the
  established harness `_register`/`_request`).

**Implementation:**
- Add module constants for the remedy tool names and breadcrumb lists (`accounting.set_quota`,
  `accounting.set_budget`) to avoid stringly-typed drift; keep the default `["allocations.list"]`.
- `_denial_response`: compute `suggested_next_actions` by the same precedence
  `_denial_detail` uses — budget `reason` first, then `QUOTA_EXCEEDED` category, else the
  existing default.
- `_denial_detail`: append/name the remedy tool for the budget and quota branches; leave the
  capacity/affinity/generic strings unchanged.

**TDD order (tests first, confirm red, then implement):**
1. Quota denial: register a resource with `quota=0`, request → assert
   `error_category == "quota_exceeded"`, `suggested_next_actions[0] == "accounting.set_quota"`,
   `"allocations.list" in suggested_next_actions`, `"accounting.set_quota" in detail`.
2. Budget denial: register with generous quota but `limit="0"`, request → assert
   `error_category == "allocation_denied"`, `data["reason"] == "budget_exceeded"`,
   `suggested_next_actions[0] == "accounting.set_budget"`, `"accounting.set_budget" in detail`.
3. Regression guard: host-capacity denial (cap=1, two requests) still
   `suggested_next_actions == ["allocations.list"]` and detail names no accounting tool — this
   assertion already exists at `test_capacity_denial_detail_is_prose_not_token`; extend it to
   assert no `"accounting."` substring in detail.

**Acceptance (reviewer-checkable):**
- Each new test fails before the implementation change and passes after.
- The named tool ids are literal registered identifiers (`accounting.set_quota` /
  `accounting.set_budget`, confirmed in `mcp/exposure.py`).
- Non-quota/budget denials are byte-for-byte unchanged.

**Guardrails before commit:** `just lint`, `just type`, then the focused test module
(`uv run pytest tests/mcp/lifecycle/test_allocations_tools.py -q`); full `just test` before push.

**Rollback:** revert the single source file; the constants and tests are additive and self-contained.
