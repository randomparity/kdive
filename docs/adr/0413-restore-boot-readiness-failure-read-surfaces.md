# ADR-0413 — Restore the boot readiness-failure read surfaces on `runs.get` (#1384)

- **Status:** Accepted
- **Date:** 2026-07-21
- **Deciders:** kdive maintainers
- **Builds on:** [ADR-0185](0185-retry-terminal-failed-step.md) (the recycle-on-retry
  step design this read works *around* without changing),
  [ADR-0230](0230-runs-get-failed-boot-evidence.md) (the surviving-failed-boot-job read
  this extends), [ADR-0374](0374-console-evidence-discovery.md) (the artifacts-table console
  ref this reuses as a failure-path source), [ADR-0179](0179-run-state-step-progress-semantics.md)
  (the step-progress read that renders a missing boot row as `pending`).

## Context

A terminal boot failure (`readiness_failure`) deletes the boot `run_steps` row so a retry
can recycle the step (ADR-0185). Every `runs.get` read derived from that row then
disappears, while the parallel surfaces (the failed boot **job**, the console **artifact**)
survive. From `runs.get` alone an agent driving a boot could not (#1384, BLACK_BOX_REVIEW
§4.1/§4.3/§5.1):

1. detect the failure by polling the step ledger — `data.steps.boot` never leaves
   `pending` (the vocabulary has no `failed` value);
2. fetch the console via the field it uses on success — `refs.console` (and
   `data.console_access`) is sourced from the deleted step's `console_evidence_artifact_id`,
   so it vanishes while only `refs.latest_console` (an artifacts-table query) survives;
3. tell whether a declared `expected_boot_failure` was reproduced or the boot failed in an
   unrelated way — the match verdict lived only in the deleted step's `matched_line`, and the
   negative case was never recorded anywhere.

ADR-0230 already surfaces the surviving failed boot job as `data.boot_readiness`
(`{job_id, status:"failed", error_category}`), so the failure is *detectable* — but the
agent-facing contract did not say so, and the console and expected-crash surfaces were still
erased.

## Decision

Keep the ADR-0185 recycle unchanged (the boot row is still deleted on failure; no `failed`
step state, no schema change). Restore all three read surfaces from state that survives the
recycle, entirely in the `runs.get` read model, and make the contract say so in the wrapper
docstring / `Field` text.

1. **Step ledger (documentation).** `data.steps.boot` stays `pending` on a recycled boot
   failure; the failure signal is `data.boot_readiness.status == "failed"`. The `runs.get`
   wrapper docstring now states this and warns against polling `steps.boot == "succeeded"`
   to detect a failed boot.

2. **Console (artifacts-table fallback).** When the boot step's
   `console_evidence_artifact_id` was recycled but a correlated console survives
   (`latest_console_ref`, the ADR-0374 artifacts-table read the failure path already
   populates), `envelope_for_run` sets `refs.console` (and `data.console_access`) from that
   surviving artifact. One field now works across success and failure. The fallback is scoped
   to a failed boot (`boot_readiness` present); a pre-boot Run keeps no `refs.console`.

3. **Expected-crash disclosure (read-time derivation).** A matched expected crash records
   `expected_crash_observed` and **succeeds** the boot step, so a surviving *failed*
   `boot_readiness` cannot coexist with a match. When the Run declared an
   `expected_boot_failure`, `data.boot_readiness.expected_crash_matched` is therefore `False`
   on this path — derived, no new persistence. The positive match is already surfaced by the
   succeeded boot step's `data.expected_boot_failure_matched_line`.

## Consequences

- No migration, no boot-handler change, no change to the recycle seam. The whole fix is in
  the read model (`mcp/tools/lifecycle/runs/common.py`) plus the agent-facing docstring
  (`registrar.py`), so the happy path is untouched.
- `refs.console` and `refs.latest_console` are equal on a readiness failure (only the boot
  snapshot exists) — the same equality ADR-0374 already documents for a non-chatty booted Run.
- `expected_crash_matched` is a *derived* verdict, not a re-run of the console match. It
  reflects the durable outcome (boot step not succeeded ⇒ the declared crash was not confirmed
  as the success outcome), which is exactly the signal an agent needs to decide whether to look
  for its declared signature or an unrelated failure.

## Alternatives considered

- **Add a `failed` run-step state / stop deleting the boot row.** Rejected: it fights the
  ADR-0185 recycle (a non-`succeeded`, non-deleted row would hang the retry's `claim_run_step`),
  touches the ledger CHECK and vocabulary, and needs a migration — a large, risky change to
  restore surfaces the surviving job + artifact already carry.
- **Persist the expected-crash verdict in the boot job's `failure_context`.** Rejected as
  redundant: the verdict is fully derivable at read time from the surviving failed job plus the
  Run's declared expectation, so persisting it adds a write path and a drift risk for no gain.
</content>
</invoke>
