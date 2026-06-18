# Spec — vmcore precondition ordering: surface `no_vmcore` first

- **Issue:** [#553](https://github.com/randomparity/kdive/issues/553)
- **ADR:** [ADR-0165](../adr/0165-vmcore-precondition-ordering.md)
- **Date:** 2026-06-17
- **Status:** Proposed

## Problem

`postmortem.triage` on a Run that never booted returns `not_found` with
`reason=no_debuginfo`. The reporter's operative cause was "no vmcore captured for this
Run." A Run's `debuginfo_ref` and `build_id` are both set by the build step, and a
captured vmcore exists only after boot + crash, so a never-booted Run lacks all three
preconditions at once. `resolve_run_vmcore_target`
(`src/kdive/mcp/tools/_vmcore_targets.py`) checks debuginfo (`:62`) before build
(`:64`) before vmcore (`:67`), so the earliest-unmet `no_debuginfo` fires even though
the caller — using a vmcore-centric tool — perceives the missing core as the gap, and
the `no_debuginfo` next actions skip past it. This is a precondition-ordering /
message-accuracy bug, not a missing reason.

## Goal

Reorder the precondition checks so a vmcore-centric caller sees the most operative
gap first: report `no_vmcore` when no core is captured, before the debuginfo and
build checks. Preserve `no_debuginfo` / `no_build` for the case where a core *is*
captured but the symbolization/provenance inputs are missing.

## Non-goals

- No new `reason` token, no change to the reason→next-actions map, no schema change,
  no migration, no resolver signature change.
- No change to the parse (`configuration_error`) or absent-Run / ungranted-project
  (bare `not_found`, no reason) branches — the no-leak seam (ADR-0123) is untouched.
- No per-caller branching; the reorder applies uniformly to all three consumers
  (`postmortem.triage`, `postmortem.crash`, `introspect.from_vmcore`).

## Behavior

In `resolve_run_vmcore_target`, after the UUID-shape, project-visibility, and
viewer-role gates, run the precondition checks in this order:

1. `raw_vmcore_key(conn, run.system_id) is None` → `_precondition_not_found(NO_VMCORE)`.
2. `run.debuginfo_ref is None` → `_precondition_not_found(NO_DEBUGINFO)`.
3. `_build_id_for_run(conn, uid) is None` → `_precondition_not_found(NO_BUILD)`.

On success, return `RunVmcoreTarget(debuginfo_ref, build_id, vmcore_ref)` as today.

Resulting reasons:

| Run state | vmcore | debuginfo | build | reason |
|-----------|--------|-----------|-------|--------|
| never booted | absent | null | absent | `no_vmcore` |
| booted, no core captured | absent | set | set | `no_vmcore` |
| core captured, debuginfo missing | present | null | — | `no_debuginfo` |
| core captured, build record missing | present | set | absent | `no_build` |

## Edge cases

- **Run never built (debuginfo null, build absent, no core):** `no_vmcore`, next
  actions `[vmcore.fetch, runs.get]`. (Issue #553's case — was `no_debuginfo`.)
- **Run built + booted, no crash captured:** `no_vmcore` (unchanged behavior).
- **Core captured, `debuginfo_ref` null:** `no_debuginfo` (a real provenance gap, no
  longer reachable for a never-booted Run). Preserved by checking vmcore first.
- **Core captured, debuginfo set, no recorded build:** `no_build`. Preserved.
- **Absent Run / ungranted project:** bare `not_found`, no reason token (no leak).
- **Malformed `run_id`:** `configuration_error` (unchanged).

## Test plan

Update `tests/mcp/test_vmcore_targets.py`:

- The acceptance case both halves: a **never-booted** Run (debuginfo null, build
  null, no vmcore row) asserts `reason == NO_VMCORE`; a **booted-but-no-core** Run
  (debuginfo set, build set, no vmcore row) asserts `reason == NO_VMCORE`.
- A **core-captured-but-debuginfo-null** Run asserts `reason == NO_DEBUGINFO` (guards
  that the reorder did not collapse the distinct reasons).
- A **core-captured, debuginfo set, build absent** Run asserts `reason == NO_BUILD`.
- The existing absent-Run (no reason), malformed-id (`configuration_error`),
  role-required, and success-path tests stay green unchanged.

The existing `test_resolve_run_vmcore_target_null_debuginfo_reason` and
`test_resolve_run_vmcore_target_missing_build_id_is_not_found` seeds must be updated
to seed a captured vmcore row (otherwise they would now report `no_vmcore`); that is
the intended contract change for those Runs only if they lack a core.
