# Terminal install failure Run transition

## Goal

Restore ADR-0179: a terminal install job failure changes a build-succeeded Run to `failed`, so
`runs.get` renders its existing ADR-0141 failure envelope.

## Design

Add `failed` as the `succeeded` Run state's sole successor in the central adjacency table and
allow that source state only for terminal install compensation. The existing transaction, Run
advisory lock-before-job-lock order, worker fence, category, and failing-job writes remain
unchanged. A terminal boot failure keeps its build-succeeded Run for ADR-0230's
`boot_readiness` read path. A requeue or fence miss leaves either Run unchanged. No read code
changes: `runs.get` already renders failed Runs through `_failed_envelope`.

## Constraints

- No migration, response field, tool parameter, retry-policy change, or boot-failure change.
- Preserve ADR-0179's build-state/run-step split and ADR-0185's failed-step recycle behavior.

## Evidence

Test the central edge, terminal install finalization fields, retryable non-terminal behavior,
terminal boot preservation, and the existing `runs.get` failure envelope after finalization.

## Rejected alternatives

- Keep a success envelope with install-failure data: conflicts with ADR-0179 and duplicates
  ADR-0141.
- Change only worker SQL: bypasses the central guarded transition table.
- Persist failed run-step rows: conflicts with ADR-0185's recycle-on-retry design.
