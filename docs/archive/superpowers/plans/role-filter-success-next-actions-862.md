# Implementation plan — Role-filter success-envelope next-actions (#862)

- **Spec:** [`../specs/2026-06-26-role-filter-success-next-actions-862.md`](../specs/2026-06-26-role-filter-success-next-actions-862.md)
- **ADR:** [ADR-0261](../../adr/0261-role-filter-success-next-actions.md)
- **Branch:** `feat/role-filter-next-actions-862`
- **Worktree:** `/home/dave/src/kdive-worktrees/role-filter-862`

## Context

Success-envelope `suggested_next_actions` are emitted unfiltered, so a granted
`allocations.request` (contributor) points at operator-only `systems.provision`. Generalize the
denial-path role-aware breadcrumb (ADR-0245/0255) to success envelopes with a project-scoped
visibility helper, then wire it into the four allocation success emit sites. Presentation only —
no schema/migration/RBAC/tool-surface change.

## Guardrail commands (run before every commit)

- `just lint` — `ruff check` + `ruff format --check`
- `just type` — `ty check` (whole tree: src + tests)
- Focused tests: `uv run python -m pytest tests/mcp/lifecycle/test_allocations_tools.py -q`
  and `uv run python -m pytest tests/mcp/test_exposure.py -q` (or wherever exposure tests live;
  discover with `rg -l "from kdive.mcp.exposure" tests/`).
- Before first push: `just test` (full suite — architecture/doc-generation tests live outside
  the touched dirs).

TDD throughout: failing test first, confirm it fails for the right reason, minimal impl, green,
refactor green. Commit one logical change at a time; end each message with the
`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.

---

## Task 1 — Project-scoped visibility helpers in `mcp/exposure.py`

**Where it fits:** the shared mechanism the four allocation sites compose with. Reuses the
existing reviewed `_TOOL_SCOPES` map and `_PROJECT_SCOPE` / `_PLATFORM_SCOPE` / `_has_platform`
/ `_ROLE_RANK` already in the module.

**Files:** `src/kdive/mcp/exposure.py`; test file for exposure (discover existing one, e.g.
`tests/mcp/test_exposure.py` / `tests/mcp/core/test_exposure.py`).

**Implementation:**

- `project_tool_visible(tool_name: str, ctx: RequestContext, project: str) -> bool` — for each
  scope in `required_scopes(tool_name)`: if it is a project scope (`_PROJECT_SCOPE`), satisfied
  iff `ctx.roles.get(project)` is not `None` and its `_ROLE_RANK` >= the scope's role rank; if it
  is a platform scope, satisfied iff `_has_platform(ctx, _PLATFORM_SCOPE[scope])`. Any-of:
  `any(...)`. Empty scope set (public) → `True`.
- `visible_next_actions(actions: Iterable[str], ctx: RequestContext, project: str) -> list[str]`
  — `[a for a in actions if project_tool_visible(a, ctx, project)]` (order preserved, no dedup).
- Factor the per-scope predicate into a small private `_project_scope_satisfied(scope, ctx,
  project)` so `project_tool_visible` mirrors the existing `tool_visible` shape. Keep functions
  small (complexity <= 8, <= 100 lines), 100-char lines, Google-style docstrings, absolute
  imports.

**TDD / tests (write first, must fail before impl):**

- `systems.provision` (`_OPERATOR`): visible iff role on `project` >= operator; contributor on
  `project` → `False`; operator on `project` → `True`.
- **Project-scoped distinction:** caller operator on project B, contributor on project A →
  `project_tool_visible("systems.provision", ctx, "A")` is `False` (the bug class). Contrast with
  the connection-scoped `tool_visible`, which returns `True` for the same ctx.
- Member of `project` with **no role** → only public tools visible (`allocations.get` viewer →
  `False`; a `PUBLIC_TOOLS` member → `True`).
- `visible_next_actions` preserves order and drops only unreachable actions; empty input → `[]`;
  all-dropped → `[]`.
- A public tool name (e.g. `resources.list`) → always `True` regardless of role.

**Acceptance:** new helpers exported; existing `tool_visible` / `visible_tool_names` untouched;
exposure tests + completeness guard green.

**Rollback:** delete the two functions + their tests; no other code depends on them yet.

---

## Task 2 — Wire the filter into the four allocation success emit sites

**Where it fits:** applies Task 1's `visible_next_actions` so no success breadcrumb advertises a
tool above the caller's role on the allocation's project.

**Files:**
- `src/kdive/mcp/tools/lifecycle/allocations/request.py` (`_grant_or_enqueue_response`)
- `src/kdive/mcp/tools/lifecycle/allocations/common.py` (`envelope_for_allocation`)
- `src/kdive/mcp/tools/lifecycle/allocations/lifecycle.py` (`_renew_response`)
- `src/kdive/mcp/tools/lifecycle/allocations/view.py` (`get`/`wait`/`list` callers of
  `envelope_for_allocation`; the `list_allocations` collection breadcrumb)
- `tests/mcp/lifecycle/test_allocations_tools.py` (update `_envelope_for_allocation` call sites
  to pass a `ctx`; add behavioural assertions)

**Implementation:**

- `_grant_or_enqueue_response(resource, project, allocation, ctx)` — gain `ctx`; wrap the
  breadcrumb: `visible_next_actions(allocation_next_actions(allocation.state), ctx, project)`.
  Update the call in `_request_response` (it already has `ctx`).
- `envelope_for_allocation(alloc, ctx, *, queue_position=None)` — gain a required positional
  `ctx`; in the success branch wrap `allocation_next_actions(alloc.state)` with
  `visible_next_actions(..., ctx, alloc.project)`. The FAILED branch is unchanged (no
  suggestions). Update the back-compat aliases at the bottom of `common.py` if they would break.
- `_renew_response(uid, outcome, ctx)` — gain `ctx`; wrap the success breadcrumb with
  `visible_next_actions(..., ctx, outcome.allocation.project)`. Update the call in
  `renew_allocation`.
- `view.py` — pass `ctx` to all three `envelope_for_allocation(...)` calls; wrap the
  `list_allocations` collection breadcrumb with `visible_next_actions([...], ctx, project)`.
- Keep `allocation_next_actions(state)` unchanged (pure candidate list).

**TDD / tests (write first, must fail before impl):**

Drive the handlers directly with an injected `RequestContext` (the project's prescribed boundary
— no transport). Use the existing test fixtures/builders in `test_allocations_tools.py`.

- Granted `request_allocation`: contributor ctx → breadcrumb has no `systems.provision`;
  operator ctx → breadcrumb unchanged (includes `systems.provision`).
- `get_allocation` on a GRANTED allocation: viewer ctx → `["allocations.get"]`; contributor →
  `["allocations.get", "allocations.release"]`; operator → all three.
- `renew_allocation` returning a GRANTED allocation: contributor ctx → no `systems.provision`.
- Operator-on-another-project: ctx operator on B + contributor on A, allocation on A →
  no `systems.provision`.
- `list_allocations`: viewer ctx → collection breadcrumb `["allocations.get"]`.
- Existing `_envelope_for_allocation` FAILED-path tests still pass once a `ctx` arg is threaded.

**Acceptance:** all four sites filtered; operator/admin behaviour byte-for-byte unchanged;
non-operator never sees `systems.provision`; focused allocation + exposure suites green; `just
lint` + `just type` clean.

**Rollback:** revert the four wiring edits and the test updates; Task 1 helpers can remain
(unused) or be reverted with Task 1.

---

## Task 3 — Full guardrail sweep + branch review

**Where it fits:** final verification before PR.

- Run `just lint`, `just type`, `just test` (full suite). Fix any architecture/doc-generation
  test that the signature changes disturb (none expected — no tool registry or schema change).
- Confirm no other module imports `envelope_for_allocation` / `_grant_or_enqueue_response` /
  `_renew_response` with the old signature (`rg` the symbols across `src/` and `tests/`).
- Adversarial branch review (`/challenge --base main`, auth focus) + `security-review` skill;
  address findings.

**Acceptance:** full suite green; challenge verdict `approve`; security-review clean.

**Rollback:** the change is presentation-only and revertible by reverting the branch; no data or
migration to undo.
