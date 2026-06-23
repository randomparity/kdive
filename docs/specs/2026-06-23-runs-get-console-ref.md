# Surface `refs.console` on `runs.get` (#735)

- **Status:** Draft
- **Date:** 2026-06-23
- **Issue:** [#735](https://github.com/randomparity/kdive/issues/735) — MCP: surface
  `refs.console` on `runs.get`. Part of #736; coordinates with #734.
- **ADR:** [ADR-0226](../adr/0226-runs-get-console-ref.md)

## Problem

`runs.get` exposes `kernel` and `debuginfo` in the envelope `refs` slot but not the
console artifact. An agent chasing crash evidence (especially the early-boot
console-crash case, #734) must make a second `artifacts.list` call against the System
and filter for the console artifact. A `refs.console` pointer on the run shortens the
path to evidence by one hop.

Black-box review (`BLACK_BOX_REVIEW.md`, defect **D5**, minor).

## Current behavior (verified against `main`)

- `_run_artifact_refs(run)` (`src/kdive/mcp/tools/lifecycle/runs/common.py:69-76`)
  emits only `kernel`/`debuginfo`, read from `Run` columns.
- The console artifact id is already produced and persisted: the boot handler writes
  `evidence_artifact_id` into the `boot` `run_steps.result`
  (`src/kdive/jobs/handlers/runs_boot.py:218,226`) on **both** the `ready` and the
  `expected_crash_observed` boot outcomes. The `ready` path writes it only when a
  console artifact was actually captured (`artifact_store` present); the
  `expected_crash_observed` path always carries one (the matched evidence).
- Console artifacts are System-owned (`owner_kind='systems'`), so the run-step result
  is the join key the read-side view needs — the id is not on the `Run` row.
- `runs.get` already reads the boot step for `SUCCEEDED` runs:
  `view.py:49-51` calls `step_progress(conn, run.id)` (which queries the `boot`
  `run_steps` row) only when `run.state is RunState.SUCCEEDED`, and passes the result
  into `envelope_for_run(..., step_progress=progress)`.

## Requirement

`runs.get` includes `refs.console` (the console `evidence_artifact_id`) when the boot
step recorded one, for both succeeded and expected-crash runs. The id resolves
directly via `artifacts.get` (the id is the artifact's primary key), one fewer hop
than `artifacts.list` + filter. A run whose boot step recorded no evidence id — not
yet booted, or a `ready` boot with no console capture — has no `refs.console` key.

## Approach

`step_progress()` already SELECTs the `boot` row `result` to extract `boot_outcome`.
Read the sibling `evidence_artifact_id` from the same row in the same query — no new
DB round-trip:

1. Add `console_evidence_artifact_id: str | None` to the `StepProgress` dataclass,
   populated in `step_progress()` from the `boot` row result (verbatim string, or
   `None` when the row is absent or carries no `evidence_artifact_id`).
2. Add an optional `console_ref: str | None = None` parameter to `_run_artifact_refs`.
   When non-`None`, it adds `refs["console"] = console_ref`. The `Run`-derived
   `kernel`/`debuginfo` keys are unchanged.
3. In `envelope_for_run`, on the `SUCCEEDED` success path only, pass
   `step_progress.console_evidence_artifact_id` into `_run_artifact_refs`. The failed
   path and the `runs.list` path (which pass no `step_progress`) call
   `_run_artifact_refs(run)` with `console_ref` defaulting to `None`, so their
   behavior is unchanged.

`refs.console` is scoped to `runs.get` because that is the only caller that loads the
boot step; `runs.list` deliberately stays cheap (no per-run step query) and is out of
scope for this issue.

## Acceptance criteria

- [ ] `runs.get` on a `SUCCEEDED` run whose `boot` step result carries
      `evidence_artifact_id` returns `refs.console` equal to that id, for a `ready`
      boot outcome and an `expected_crash_observed` boot outcome.
- [ ] `runs.get` on a run with no recorded boot evidence (no boot step, or a boot step
      whose result has no `evidence_artifact_id`) returns **no** `console` key in
      `refs`.
- [ ] The returned id resolves directly via `artifacts.get` (it is the artifact id,
      no list+filter needed). Verified by asserting the id equals the persisted
      console artifact's primary key.
- [ ] `kernel`/`debuginfo` refs are unaffected; `runs.list` and failed-run envelopes
      are unchanged.

## Edge cases

- **Boot step absent** (not yet booted) → `step_progress` reports `boot="pending"`,
  `console_evidence_artifact_id=None` → no `refs.console`. (Asserts "absent before
  boot".)
- **`ready` boot with no console capture** (`artifact_store` was `None`) → boot
  result has no `evidence_artifact_id` key → `None` → no `refs.console`.
- **Non-`SUCCEEDED` run** → `runs.get` passes `step_progress=None`, so the success
  branch is not reached and the boot step is never queried → no `refs.console`. A
  `FAILED` run renders through `_failed_envelope`, which never sets `console`.
- **Malformed `evidence_artifact_id`** (non-string) → `_optional_str` yields `None`,
  same as absent. No exception, no key.

## Out of scope

- `runs.list` console refs (would add a per-run step query to the list path).
- Resolving/validating the artifact exists at view time (the id is a pointer; a stale
  id is the caller's `artifacts.get` to discover, consistent with `kernel`/`debuginfo`
  which are also unvalidated pointers).
- #734's triage/`vmcore.fetch` guidance that *reads* this `evidence_artifact_id`
  (separate issue; this issue only owns the write to `runs/common.py`).
