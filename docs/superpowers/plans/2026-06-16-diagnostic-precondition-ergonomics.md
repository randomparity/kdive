# Plan — Diagnostic-tool precondition ergonomics (#487, D7)

- **Spec:** [../../specs/2026-06-16-diagnostic-precondition-ergonomics.md](../../specs/2026-06-16-diagnostic-precondition-ergonomics.md)
- **ADR:** [../../adr/0142-diagnostic-precondition-ergonomics.md](../../adr/0142-diagnostic-precondition-ergonomics.md)
- **Execution mode:** direct (tightly coupled; one shared reason-token contract
  threads the resolver, the shared helper, and four call sites). No parallel
  implementers.

Guardrails before every commit (the CI-hard-gated subset that touches this work):
`just lint`, `just type`, and the focused tests
(`uv run python -m pytest tests/mcp/test_vmcore_targets.py tests/mcp/lifecycle/test_vmcore_tools.py tests/mcp/debug/test_introspect_tools.py tests/mcp/debug/test_debug_tools.py -q`).
Full suite + `just docs-check` before the first push (step 7).

## Task 1 — Granular vmcore precondition reason tokens

**Files:** `src/kdive/mcp/tools/_vmcore_targets.py`,
`tests/mcp/test_vmcore_targets.py`.

In `resolve_run_vmcore_target`, replace the three shared `_target_not_found()`
raises for *precondition* misses (null `debuginfo_ref`, no `build`, no vmcore) with
distinct `not_found` `CategorizedError`s, each `details={"reason": <token>}`:
`no_debuginfo`, `no_build`, `no_vmcore`. The absent-Run / ungranted-project raise
stays the bare `_target_not_found()` (no reason — no membership leak). Add module
constants for the three reason tokens and a `_VMCORE_NEXT_ACTIONS:
dict[str, list[str]]` mapping. Add a public helper
`vmcore_target_failure(run_id: str, exc: CategorizedError) -> ToolResponse` that
calls `ToolResponse.failure_from_error(run_id, exc,
suggested_next_actions=_VMCORE_NEXT_ACTIONS.get(reason))` where `reason` is read
from `exc.details`; absent/unknown reason → no next actions.

**TDD:** extend `tests/mcp/test_vmcore_targets.py` — each precondition raises
`NOT_FOUND` with the expected `details["reason"]`; the absent-Run case carries no
`reason`. Add a unit test for `vmcore_target_failure` asserting category, the
reason-keyed `suggested_next_actions`, and suppressed `detail == "not found"`.

**Acceptance:** `details["reason"]` is `no_debuginfo`/`no_build`/`no_vmcore` for the
three precondition misses; absent-Run has no `reason`; helper maps reason → the
spec's next-action lists; `detail` stays `"not found"`.

## Task 2 — Route the three vmcore callers through the helper

**Files:** `src/kdive/mcp/tools/lifecycle/vmcore.py` (`_postmortem_crash`),
`src/kdive/mcp/tools/debug/introspect.py` (`introspect_from_vmcore`),
`tests/mcp/lifecycle/test_vmcore_tools.py`,
`tests/mcp/debug/test_introspect_tools.py`.

Replace the `except CategorizedError: return ToolResponse.failure_from_error(run_id,
exc)` arms that wrap `resolve_run_vmcore_target` with
`return vmcore_target_failure(run_id, exc)`. Leave the *second* `except` arms (port
provenance / drgn faults) unchanged — those are not target-resolution misses.
`_postmortem_triage` inherits via `_postmortem_crash`.

**TDD:** add behavior tests — a Run with no captured vmcore through
`postmortem.triage` and `introspect.from_vmcore` returns `not_found` with
`data.reason == "no_vmcore"` and `suggested_next_actions == ["vmcore.fetch",
"runs.get"]`.

**Acceptance:** the three tools surface the reason token + next actions; the
non-resolution failure arms are untouched.

## Task 3 — start_session precondition detail + next actions

**Files:** `src/kdive/mcp/tools/debug/sessions.py` (`_attach_preconditions`),
`tests/mcp/debug/test_debug_tools.py`.

For the three *caller* preconditions in `_attach_preconditions`, pass
`detail=<author prose>` and `suggested_next_actions=<list>` to the failure. The
`_config_error` helper (`ToolResponse.failure(..., CONFIGURATION_ERROR)`) does not
take `detail`/`next_actions`, so call `ToolResponse.failure(...)` directly for these
three branches (keeping the `data` token). Use the spec's prose + next-action lists.
Leave System-absent / not-ready / transport-conflict branches as-is.

**TDD:** add behavior tests asserting each of the three branches returns
`configuration_error` with the expected `data` token, a non-empty `detail`, and the
expected `suggested_next_actions`.

**Acceptance:** each of the three branches carries the `data` token + author detail +
next actions; the internal/transient branches are unchanged.

## Rollback / cleanup

Pure additive behavior change — no migration, no schema, no entrypoint change. Revert
is `git revert` of the feature commits. No generated tool-reference change (tool
descriptions unchanged) — confirm with `just docs-check` before push.
