# ADR-0179: Run.state means build-succeeded; run_steps carry install/boot

Status: Accepted

## Context

`RunState.SUCCEEDED` records that the **build** step finished (`finalize_build` writes the
`run_steps(step='build', state='succeeded')` row and drives `running → succeeded` in one
per-Run transaction). Install and boot are separate idempotent step jobs admitted *after*
the Run is `succeeded` (`runs.install` / `runs.boot`), persisted only in `run_steps`. Debug
attach correctly requires both `RunState.SUCCEEDED` and a succeeded `boot` step. The split
is internally guarded, but the public status *name* invites a caller to read a `succeeded`
Run as fully built+installed+booted (#564).

`runs.get` is the discovery surface, yet it surfaces no install/boot step status, and its
`suggested_next_actions` for a bound `SUCCEEDED` Run is always `["runs.get",
"runs.install"]` — even after install and boot already succeeded. The issue forbids a
partial `RunState` rename without an ADR, since the name is a persisted public contract.

## Decision

Keep `RunState` unchanged (build-phase milestones; no rename, no migration) and make the
build-vs-install-vs-boot relationship explicit and discoverable on `runs.get`.

1. **Documented relationship.** `Run.state.succeeded` means *build succeeded*. Install and
   boot status is answered only by `run_steps`. A `build` row exists iff the Run is
   `SUCCEEDED`; install/boot rows go `running → succeeded` (`claim_run_step` /
   `complete_run_step`) and a failed step fails the Run to `FAILED`, so `run_steps` only
   ever holds `running` or `succeeded` (ADR-0171). This is stated in the `RunState`
   docstring and the `runs.get` docs.

2. **`data.steps` on a `SUCCEEDED` Run.** `runs.get` adds a fixed-key map
   `{"build": s, "install": s, "boot": s}` where `s` is the persisted `run_steps.state`
   (`succeeded`/`running`) or `pending` when no row exists. `pending` is a read-surface
   synthesis; the persisted vocabulary stays `{running, succeeded}`. The map is absent on
   non-`SUCCEEDED` Runs (pre-build, terminal-canceled, or a failure envelope). One extra
   single-row-set query on the `get` path; `runs.list` is unchanged.

3. **Progression in `suggested_next_actions`.** For a `SUCCEEDED` Run the second action
   follows the real progression: unbound→`runs.bind`; built→`runs.install`;
   installed→`runs.boot`; booted normally→`debug.start_session`. A boot whose recorded
   `boot_outcome == "expected_crash_observed"` (read from the `boot` step result) is routed
   to `postmortem.triage` / `vmcore.fetch`, matching the failure the debug plane returns for
   a live attach on such a boot. The branch keys on the **observed** outcome, not the Run's
   create-time `expected_boot_failure`: a Run that expected a crash but booted normally
   records `boot_outcome: "ready"` and is live-debuggable.

A `running` install/boot is read as persisted (no liveness reinterpretation, matching
ADR-0176) and still recommends the same forward tool (the step jobs are idempotent); the
`data.steps` map shows the in-flight claim.

No state machine, schema, transport, or migration change.

## Consequences

- A caller of `runs.get` on a `SUCCEEDED` Run can tell whether install and boot have
  happened and what the next lifecycle tool is, without inferring it from `Run.state`
  alone — closing the #564 discoverability gap additively.
- The public `RunState` contract (DB CHECK, envelope `status`, every caller) is untouched;
  the semantics are clarified in docs rather than encoded in a new name.
- `runs.get` takes one extra small query on the `get` path only.

## Considered & rejected

- **Rename/reshape `RunState`** (e.g. `built`, or a composite build/install/boot status).
  The status name is a persisted public contract; the issue forbids a partial rename
  without a dedicated ADR, and the discoverability problem is solvable additively.
- **Refine only `suggested_next_actions`, no `steps` map.** Weaker for "expose enough step
  status without guessing" and gives no signal for an in-flight `running` step.
- **A single derived `phase` enum.** Introduces a new vocabulary, collapses
  `running` vs `succeeded`, and duplicates the explicit per-step map.
- **Reinterpret a stale `running` claim as failed on read.** Read paths report persisted
  state (ADR-0176); `claim_run_step` owns staleness reaping.
