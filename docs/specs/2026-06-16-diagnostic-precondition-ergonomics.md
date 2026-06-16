# Diagnostic-tool precondition ergonomics (#487, D7)

- **Status:** Draft
- **Date:** 2026-06-16
- **ADR:** [0142](../adr/0142-diagnostic-precondition-ergonomics.md)
- **Issue:** #487

## Problem

Three diagnostic read tools return a correct `error_category` but terse,
non-actionable detail when a precondition is unmet — a regression from the guidance
the envelope gives on the allocation/discovery surfaces (ADR-0132). A black-box
(MCP-only) agent cannot learn *which* precondition is unmet or which tool to call
next.

1. **`postmortem.triage` / `postmortem.crash` / `introspect.from_vmcore`** on a Run
   that is missing any one of its three target prerequisites — `debuginfo_ref` is
   null, no `build` step is recorded, or no vmcore was captured — all raise the
   *same* generic `not_found` `CategorizedError`
   (`_target_not_found()` in `_vmcore_targets.py`) with no `data` token and no
   `suggested_next_actions`. The three callers pass it straight through
   `ToolResponse.failure_from_error(run_id, exc)`.

2. **`debug.start_session`** on a Run that is not live-debuggable returns
   `configuration_error` carrying only a `data` reason token
   (`{"reason": "boot_first"}` etc.) — no `detail` prose, no `suggested_next_actions`.

## Constraint: the no-leak seam (ADR-0123)

`not_found` is a **suppressed** category: `suppressed_detail()` overwrites any
`detail` on a `not_found`/`authorization_denied` failure with the fixed constant
`"not found"` / `"access denied"`, so resource existence cannot leak through
`detail`. **`data` is not suppressed.** Therefore the granular, actionable guidance
for the (`not_found`) vmcore preconditions must ride in `data` (a structured `reason`
token) and `suggested_next_actions` — not in `detail`. For `debug.start_session` the
category is `configuration_error` (not suppressed), so author-controlled `detail`
prose *does* reach the caller and is paired with the existing `data` reason token.

All surfaced text is **author-controlled** — a fixed mapping keyed off an internal
reason token. No guest output, exception message, or resource identifier is
interpolated (matches `allocations._denial_detail`).

## Design

### vmcore target resolution (`_vmcore_targets.py`)

`resolve_run_vmcore_target` raises a distinct `not_found` `CategorizedError` per
unmet precondition, each carrying `details={"reason": <token>}`:

| precondition (in resolve order) | reason token | suggested_next_actions |
|---|---|---|
| `debuginfo_ref` is null | `no_debuginfo` | `runs.get`, `runs.build` |
| no recorded `build` step | `no_build` | `runs.build`, `runs.get` |
| no captured vmcore | `no_vmcore` | `vmcore.fetch`, `runs.get` |

The absent-Run / ungranted-project branch keeps the bare `not_found` with no reason
(membership must not leak — the envelope stays byte-identical to a genuinely-absent
Run). A new shared helper `vmcore_target_failure(run_id, exc) -> ToolResponse` maps a
caught `CategorizedError` to the envelope, attaching the reason-keyed
`suggested_next_actions` (an empty list when the reason is absent or unknown). All
three callers replace their bare `failure_from_error` call with this helper.

`detail` for these stays the suppressed `"not found"` constant — the actionable
content is in `data.reason` + `suggested_next_actions`, which the seam does not touch.

### `debug.start_session` preconditions (`sessions.py::_attach_preconditions`)

Each precondition pairs the existing `data` reason/`current_status` token with an
author-controlled `detail` and `suggested_next_actions`:

| precondition | data token | detail (author-controlled) | next actions |
|---|---|---|---|
| Run not `succeeded` | `current_status=<state>` | "run is not booted; it must reach a successful boot before a live session" | `runs.get` |
| no successful boot result | `reason=boot_first` | "run has no successful boot; boot it before starting a live session" | `runs.boot`, `runs.get` |
| booted into expected crash | `reason=expected_crash_not_live_debuggable` | "run booted into an expected crash and is not live-debuggable; analyze its captured core instead" | `postmortem.triage`, `vmcore.fetch` |

The System-not-present, System-not-ready, and transport-conflict branches are
internal/transient races (not a *caller* precondition the agent can act on by calling
a different tool with the same id) and are left as-is.

## Acceptance

- Each of the three vmcore preconditions yields a `not_found` envelope whose
  `data.reason` names the unmet precondition and whose `suggested_next_actions` names
  the next tool(s). The absent-Run case carries no reason and no next actions.
- Each of the three `debug.start_session` caller preconditions yields a
  `configuration_error` envelope carrying both the `data` token *and* an
  author-controlled `detail` + `suggested_next_actions`.
- `detail` on the `not_found` paths is unchanged (`"not found"`); no guest/exception
  text or resource id is interpolated anywhere (no-leak seam intact).
- All `suggested_next_actions` are literal valid registered tool names.

## Out of scope

- The malformed-`run_id` `configuration_error` branch (already a parse failure, not a
  precondition).
- Internal/transient `start_session` branches (System absent/not-ready, transport
  conflict).
- Any schema, migration, or DB change (none required).
