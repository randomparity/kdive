# ADR 0142 — Diagnostic-tool precondition ergonomics

- **Status:** Accepted
- **Date:** 2026-06-16
- **Deciders:** kdive maintainers

## Context

Black-box MCP evaluation (D7, #487) found three diagnostic read tools return a
correct `error_category` but terse, non-actionable failure envelopes when a
precondition is unmet — a regression from the guidance ADR-0132 added to the
allocation/discovery surfaces.

- `postmortem.triage` / `postmortem.crash` / `introspect.from_vmcore` resolve a Run's
  vmcore target through `resolve_run_vmcore_target` (`_vmcore_targets.py`). A null
  `debuginfo_ref`, a missing `build` step, and an uncaptured vmcore all raise the
  *same* generic `not_found` `CategorizedError` with no `data` token and no
  `suggested_next_actions`; the three callers pass it through
  `ToolResponse.failure_from_error` unchanged.
- `debug.start_session` precondition failures (`_attach_preconditions`) return
  `configuration_error` carrying only a `data` reason token (`boot_first`,
  `expected_crash_not_live_debuggable`, `current_status`) — no `detail` prose, no
  `suggested_next_actions`.

The no-leak seam (ADR-0123) suppresses `detail` for `not_found` /
`authorization_denied` (a fixed constant replaces any author message so resource
existence cannot leak), but **does not** suppress `data` or `suggested_next_actions`.

## Decision

Make each diagnostic precondition failure self-describing with author-controlled,
no-leak-safe content.

1. **vmcore preconditions carry a structured reason token + next actions.**
   `resolve_run_vmcore_target` raises a distinct `not_found` error per precondition,
   each with `details={"reason": <token>}`: `no_debuginfo`, `no_build`, `no_vmcore`.
   A new shared helper `vmcore_target_failure(run_id, exc)` maps the caught error to
   the envelope and attaches the reason-keyed `suggested_next_actions`
   (`no_debuginfo`→`[runs.get, runs.build]`, `no_build`→`[runs.build, runs.get]`,
   `no_vmcore`→`[vmcore.fetch, runs.get]`). The absent-Run / ungranted-project branch
   stays a bare `not_found` with no reason token so the envelope is byte-identical to
   a genuinely-absent Run (no membership leak). The three callers route through the
   helper. `detail` on these stays the suppressed `"not found"` constant; the
   actionable content is in `data.reason` + `suggested_next_actions`.

2. **`debug.start_session` preconditions carry author-controlled `detail` + next
   actions** alongside the existing `data` token, since `configuration_error` is not
   suppressed. The `not booted`, `boot_first`, and `expected_crash_not_live_debuggable`
   branches each get a fixed prose `detail` and `suggested_next_actions`.

All surfaced text is a fixed mapping keyed off an internal token — no guest output,
exception message, or resource identifier is interpolated (mirrors
`allocations._denial_detail`, ADR-0132).

## Consequences

- A black-box agent that hits a vmcore precondition learns the unmet precondition
  (`data.reason`) and the next tool to call; a `debug.start_session` precondition also
  gets prose.
- The reason token becomes part of the diagnostic-tool contract. A new precondition
  must add a token + a next-actions mapping entry (an unmapped reason degrades to an
  empty action list, not an error).
- No schema, migration, DB, or entrypoint change. Generated tool reference is
  unaffected (descriptions unchanged).
- The no-leak seam is untouched: `not_found` `detail` stays suppressed; only `data`
  (already unsuppressed) and `suggested_next_actions` carry the new content.

## Considered & rejected

- **Put the vmcore guidance in `detail`.** `detail` is overwritten by the no-leak
  seam for `not_found`. Either we would surface nothing (current behavior) or we would
  have to exempt these paths from suppression — reopening the ADR-0123 leak surface.
  Rejected: `data` + `suggested_next_actions` carry the content with the seam intact.
- **Downgrade the vmcore preconditions to `configuration_error` so `detail` flows.**
  A Run that exists but lacks a captured core is an absent *target resource*, which is
  `not_found` by ADR-0097, not malformed input. Re-categorizing to expose `detail`
  would misclassify the failure and break the malformed-vs-absent distinction the two
  helpers deliberately keep separate. Rejected.
- **A bespoke detail per precondition derived from the Run's state.** Risks
  interpolating resource state into a suppressed-category envelope. Rejected in favor
  of a fixed token→actions mapping.
