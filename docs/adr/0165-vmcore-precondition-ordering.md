# ADR 0165 — Surface the vmcore precondition first in `resolve_run_vmcore_target`

- **Status:** Accepted
- **Date:** 2026-06-17
- **Deciders:** kdive maintainers

## Context

`resolve_run_vmcore_target` (`src/kdive/mcp/tools/_vmcore_targets.py`) is the shared
precondition resolver for the three vmcore-centric read tools — `postmortem.triage`,
`postmortem.crash`, and `introspect.from_vmcore`. ADR-0142 gave it a structured
`reason` token per unmet precondition (`no_debuginfo`, `no_build`, `no_vmcore`) and
reason-keyed `suggested_next_actions`, but kept the check order from the original
code: `debuginfo_ref is None` → `no_debuginfo`, then no recorded build → `no_build`,
then no captured core → `no_vmcore`.

A Run's `debuginfo_ref` and `build_id` are both written by the build step
(`jobs/handlers/runs_shared.py`), and a captured vmcore only exists after the Run
boots and crashes. So a Run that never built/booted lacks all three preconditions at
once. With the build-first order, such a Run reports `reason=no_debuginfo` even though
the caller invoked a tool whose entire purpose is to analyze a *captured core* and
whose operative gap, from the caller's vantage, is "no vmcore captured for this Run"
(#553). The reason is technically true (debuginfo is the earliest unmet step) but
names a step the caller was not reaching for, and the `no_debuginfo` next actions
(`runs.get`, `runs.build`) point past the missing core.

The reason token is part of the diagnostic-tool contract (ADR-0142), so changing
which token a never-booted Run reports is a deliberate contract change, not an
implementation detail.

## Decision

Reorder the precondition checks in `resolve_run_vmcore_target` so the most operative
gap for these vmcore-centric callers surfaces first: check the captured vmcore
**before** debuginfo and build.

New order, after the parse / visibility / role gates:

1. No captured vmcore row → `not_found`, `reason=no_vmcore`.
2. Null `debuginfo_ref` → `not_found`, `reason=no_debuginfo`.
3. No recorded build step → `not_found`, `reason=no_build`.

A Run that never booted now reports `no_vmcore` with `suggested_next_actions`
`[vmcore.fetch, runs.get]` — the user-facing entry to the capture flow. A Run that
*did* capture a core but is missing debuginfo or its build record (the rarer
"core present, build artifacts absent" provenance gap) still reports the precise
`no_debuginfo` / `no_build` reason, because those checks still run when a vmcore
exists. The absent-Run / ungranted-project branch is unchanged: it stays a bare
`not_found` with no reason token (no membership leak, ADR-0123).

No new reason token, no schema change, no migration, no signature change. The vmcore
lookup (`raw_vmcore_key`) simply moves ahead of the two row-field checks.

## Consequences

- A never-booted Run triaged via `postmortem.triage` (and `postmortem.crash` /
  `introspect.from_vmcore`) now reports `no_vmcore`, matching the caller's perception
  and the tool's purpose; the next action points at `vmcore.fetch`.
- The `no_debuginfo` / `no_build` reasons narrow to their precise meaning: a core was
  captured but the symbolization/provenance inputs are missing — no longer a proxy
  for "this Run never built."
- The change is uniform across all three resolver consumers; all are vmcore-centric,
  so vmcore-first is correct for each.
- `raw_vmcore_key` now runs for Runs that previously short-circuited on a null
  `debuginfo_ref`. It is a single indexed lookup on the same connection; the added
  cost is one query on the precondition-miss path only.

## Considered & rejected

- **Keep the build-first order; reword `no_debuginfo`'s prose to say "not built
  yet."** (Issue #553 option 2.) The `not_found` `detail` is suppressed to a fixed
  constant by the no-leak seam (ADR-0123); the only caller-visible signal is the
  `reason` token and its next actions. Rewording prose the caller never sees would
  not fix the misleading token, and renaming the token to imply "not built" would
  still point a vmcore-centric caller at the wrong gap. Rejected.
- **Add a fourth, combined token (e.g. `not_built`) for the all-three-absent case.**
  Adds a token whose next action is identical to `no_vmcore`'s entry point and
  duplicates the build-first ordering question. Rejected as redundant.
- **Branch the order per caller (introspect vs. postmortem).** All three consumers
  need a captured core; none benefits from a debuginfo-first report. A per-caller
  split adds a parameter and two code paths for no behavioral gain. Rejected.
