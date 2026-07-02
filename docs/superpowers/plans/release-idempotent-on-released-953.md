# Plan: idempotent `allocations.release` on an already-released grant (#953)

Derived from `docs/superpowers/specs/2026-07-01-issue-953-release-idempotency-design.md` and
[ADR-0293](../../adr/0293-idempotent-release-on-released.md). Single tightly-coupled change;
implement directly in this session with TDD (not subagent-driven).

Guardrails (run before each commit): `just lint`, `just type`, `just test` (or the focused
test file first, full suite before push). Doc guards already run for the ADR/spec commit.

## Task 1 — Failing tests for the new release contract

Where it fits: locks the observable contract before the source change (spec criteria 1-5).

Files: `tests/mcp/lifecycle/test_allocations_tools.py`.

Steps:

1. **Rewrite** `test_release_terminal_allocation_is_stale_handle` (currently seeds `RELEASED`
   and asserts `stale_handle`) into `test_release_released_allocation_is_idempotent_ok`: seed
   `RELEASED`, call `release_allocation`, assert `resp.status == "released"` and
   `resp.error_category is None`. Then assert **zero** additional `audit_log` rows and **zero**
   additional `ledger` rows exist for the allocation beyond what seeding produced (seeding via
   `_seed_alloc` inserts the row directly in `RELEASED` with no release audit/ledger, so both
   counts are 0). This proves the no-op writes nothing.
2. **Add** `test_release_expired_allocation_is_stale_handle`: seed `EXPIRED`, assert
   `resp.status == "error"`, `resp.error_category == "stale_handle"`,
   `resp.data["current_status"] == "expired"`.
3. **Add** `test_release_failed_allocation_is_stale_handle`: seed `FAILED`, same shape with
   `current_status == "failed"`.
4. Confirm `_seed_alloc` accepts `AllocationState.EXPIRED`/`FAILED` (it inserts any non-
   `REQUESTED` state with a placed `resource_id`); if the 0016 CHECK rejects a directly-seeded
   terminal row, fall back to seeding `ACTIVE` then `ALLOCATIONS.update_state` through the
   legal path. Verify by running the new tests.

Acceptance: the three new/rewritten tests **fail** for the right reason (idempotent test:
`stale_handle` != `released`; expired/failed: still pass under current code, so they are
regression guards, not red — that is expected and fine). Run:
`uv run python -m pytest tests/mcp/lifecycle/test_allocations_tools.py -q -k "release_released or release_expired or release_failed"`.

Rollback: revert the test edits.

## Task 2 — Break-glass idempotent-release test

Where it fits: spec criterion 5 — the shared `_release_locked` means break-glass inherits the
idempotent outcome.

Files: `tests/mcp/ops/test_breakglass.py`.

Steps:

1. Find the existing force-release success test as a template. Add
   `test_force_release_already_released_is_idempotent_ok`: seed a `RELEASED` allocation, call
   the force-release handler as a platform admin, assert `status == "released"`. Also assert a
   platform accountability audit row was written (the break-glass path records it before
   release), matching the existing force-release test's audit assertion pattern.

Acceptance: test fails under current code (`stale_handle`), passes after Task 3. Run the file:
`uv run python -m pytest tests/mcp/ops/test_breakglass.py -q -k idempotent`.

Rollback: revert the test edit.

## Task 3 — Split the terminal branch in `_release_locked`

Where it fits: the minimal source change that makes Tasks 1-2 green (spec Decision).

Files: `src/kdive/services/allocation/release.py`.

Steps:

1. Replace the single `if current.state in _TERMINAL:` block (lines ~240-245) with two checks:
   - `if current.state is AllocationState.RELEASED: return ReleaseOutcome(released=True)` — the
     idempotent no-op: no transition, no audit, no `stamp_active_ended`, no `reconcile`.
   - `if current.state in (AllocationState.EXPIRED, AllocationState.FAILED): return
     ReleaseOutcome(released=False, category=STALE_HANDLE, current_status=current.state.value)`
     — unchanged behavior for the not-requested terminal outcomes.
2. Add a short comment citing ADR-0293 explaining why `released` is idempotent-ok while
   `expired`/`failed` stay `stale_handle`, and why the no-op writes nothing (ADR-0040 §4).
3. Do **not** touch `reclaim_under_lock` (the reaper) — its `_TERMINAL` check stays.
4. Keep `_TERMINAL`/`_RELEASABLE` module constants; they are still used by `reclaim_under_lock`
   and the downstream `not in (*_RELEASABLE, RELEASING)` config-error guard. Verify no unused-
   import/constant lint after the edit.

Acceptance: all tests from Tasks 1-2 green; existing `test_release_granted_allocation`,
`test_release_active_allocation`, `test_release_requested_allocation_cancels_with_no_credit`,
`test_release_illegal_transition_backstop_returns_failure` still green. Run:
`uv run python -m pytest tests/mcp/lifecycle/test_allocations_tools.py tests/mcp/ops/test_breakglass.py -q`.

Rollback: `git checkout src/kdive/services/allocation/release.py`.

## Task 4 — Update the `allocations.release` wrapper docstring

Where it fits: the agent-facing contract must state the idempotent behavior (AGENTS.md: the
wrapper docstring is the agent-facing contract, not the handler).

Files: `src/kdive/mcp/tools/lifecycle/allocations/registrar.py` — the `allocations.release`
wrapper `allocations_release`, docstring currently `"""Release an active allocation."""`
(line ~130). This is the agent-facing contract per AGENTS.md, NOT the handler
`release_allocation`.

Generated-doc coupling (verified): `docs/guide/reference/allocations.md` is generated from the
wrapper docstrings by `scripts/gen_tool_reference.py` and gated by `just docs-check`; the
packaged MCP doc-resource snapshots are gated by `just resources-docs-check` (ADR-0151). Both
must be regenerated after the docstring change or CI fails.

Steps:

1. Extend the **wrapper** docstring to state: releasing an already-`released` grant returns
   `ok` (idempotent, a no-op); a completed `systems.teardown` may leave the allocation auto-
   released by the reconciler, so a step-9 release can be a no-op `ok`. `expired`/`failed`
   still return `stale_handle`. Keep prose plain (no "robust"/"comprehensive" — doc-style
   guard).
2. Regenerate: `just docs` (writes `docs/guide/reference/*`) and `just resources-docs` (writes
   the packaged snapshots). Review the diff — only the release entry should change.
3. Verify: `just docs-check` and `just resources-docs-check` pass.

Acceptance: `just lint type test`, `just docs-check`, `just resources-docs-check` all green.
The regenerated `allocations.md` + doc-resource snapshots are reviewed and committed in the
same commit as the docstring.

Rollback: revert the docstring + regenerated docs.

## Task 5 — Full guardrails + branch review

1. Run the full suite: `just lint`, `just type`, `just test`.
2. Adversarial branch review (`/challenge --base main`), address findings.
3. Security review if the repo provides one.

## Commit sequence

- (done) `docs(allocation): ADR-0293 idempotent release on released grant`
- `test(allocation): pin idempotent release + expired/failed stale_handle` (Tasks 1-2, red)
- `fix(allocation): idempotent allocations.release on released grant` (Task 3, green) — Closes-
  adjacent; the fix commit.
- `docs(allocation): document idempotent release on the tool wrapper` (Task 4) — fold the
  regenerated reference doc here if any.
