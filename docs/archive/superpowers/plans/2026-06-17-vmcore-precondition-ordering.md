# Plan — vmcore precondition ordering (#553)

- **Spec:** [vmcore-precondition-ordering](../../specs/2026-06-17-vmcore-precondition-ordering.md)
- **ADR:** [ADR-0165](../../adr/0165-vmcore-precondition-ordering.md)
- **Date:** 2026-06-17

Scope: one production function (`resolve_run_vmcore_target` in
`src/kdive/mcp/tools/_vmcore_targets.py`) and its tests
(`tests/mcp/test_vmcore_targets.py`). No schema, no migration, no signature change,
no new reason token. Tightly coupled to one function, so implemented directly in this
session (not subagent-dispatched). TDD throughout.

## Task 1 — Tests for the reordered preconditions (failing first)

Where it fits: encodes the #553 acceptance criteria and the ADR-0165 contract before
the reorder lands, so the reorder is driven by a red→green transition.

Files: `tests/mcp/test_vmcore_targets.py`.

Steps:

1. Add `test_resolve_run_vmcore_target_never_booted_reports_no_vmcore`: seed a Run
   with `debuginfo_ref=None`, `build_id=None`, and **no** vmcore row; assert
   `category is NOT_FOUND` and `details["reason"] == NO_VMCORE`. (Currently fails:
   reports `NO_DEBUGINFO`.)
2. Add `test_resolve_run_vmcore_target_booted_no_core_reports_no_vmcore`: seed a Run
   with `debuginfo_ref` set, `build_id` set, and **no** vmcore row; assert
   `reason == NO_VMCORE`. (Currently already passes — guards the booted-but-no-core
   half stays `no_vmcore` and proves the two acceptance cases report distinct/accurate
   reasons relative to task-1 case 1.)
3. Update `test_resolve_run_vmcore_target_null_debuginfo_reason`: seed a **captured
   vmcore row** for the System so the debuginfo check is reachable; keep
   `debuginfo_ref=None`, `build_id="deadbeef"`; assert `reason == NO_DEBUGINFO`.
   (Currently passes without the vmcore row; after the reorder it would report
   `no_vmcore` without the row — this update guards the distinct `no_debuginfo`
   reason for the core-present case.)
4. Update `test_resolve_run_vmcore_target_missing_build_id_is_not_found`: it already
   seeds a vmcore row (`_seed_vmcore_row`) and `debuginfo_ref` set, `build_id=None`;
   confirm it still asserts `reason == NO_BUILD`. No seed change needed — verify it
   stays green after the reorder.

Acceptance: tasks-1 new tests for the never-booted case fail for the expected reason
(`NO_DEBUGINFO` instead of `NO_VMCORE`) before task 2; all other tests in the file
still pass (the null-debuginfo test passes once its seed adds the vmcore row, even
before the reorder, because vmcore-present + null-debuginfo already yields
`no_debuginfo` under the current order too).

Guardrails: `uv run python -m pytest tests/mcp/test_vmcore_targets.py -q`.

## Task 2 — Reorder the precondition checks

Where it fits: the production change the spec/ADR describe.

Files: `src/kdive/mcp/tools/_vmcore_targets.py`.

Steps:

1. In `resolve_run_vmcore_target`, after the `require_role(...)` line, reorder to:
   resolve `vmcore_ref = await raw_vmcore_key(conn, run.system_id)` and raise
   `_precondition_not_found(NO_VMCORE)` if `None`; then `if run.debuginfo_ref is
   None: raise _precondition_not_found(NO_DEBUGINFO)`; then resolve `build_id` and
   raise `_precondition_not_found(NO_BUILD)` if `None`. Return
   `RunVmcoreTarget(run.debuginfo_ref, build_id, vmcore_ref)`.
2. Update the function docstring's parenthetical ordering ("no captured core, null
   `debuginfo_ref`, no recorded build") to match the new order and cite ADR-0165
   alongside ADR-0097.

Acceptance: full `tests/mcp/test_vmcore_targets.py` green, including the task-1
never-booted test now reporting `NO_VMCORE`.

Guardrails: `just lint`, `just type`, then
`uv run python -m pytest tests/mcp/test_vmcore_targets.py tests/mcp/lifecycle/test_vmcore_tools.py tests/mcp/debug/test_introspect_tools.py -q`.

## Task 3 — Sweep dependent docstrings / tool docs

Where it fits: the resolver's behavior is described in two caller docstrings and the
ADR-0142-era comment block; keep them accurate.

Files: `src/kdive/mcp/tools/_vmcore_targets.py` (already in task 2),
`src/kdive/mcp/tools/debug/introspect.py` (the `introspect_from_vmcore` docstring
lists "null `debuginfo_ref`, no recorded `build` step, or no captured core").

Steps:

1. Reword the `introspect_from_vmcore` docstring precondition list to lead with "no
   captured core" so the prose matches the surfaced reason order. (Wording only — no
   behavior change; the resolver is shared.)
2. Check whether any generated tool-doc snapshot references these reasons; if a
   `just`-generated doc changed, regenerate it.

Acceptance: `just docs-check`, `just lint`, `just type` green; no stale prose.

Rollback/cleanup: single-function revert restores prior order; no migration or state
to undo. The branch is the only artifact.
