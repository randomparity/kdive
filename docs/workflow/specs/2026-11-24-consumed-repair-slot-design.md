# Skip payload derivation for consumed repair slots

## Problem

The external-boot repair lane derives a stable admission identity, then rebuilds
its payload before enqueue discovers the existing durable slot. Exhausted and
terminal slots therefore repeat activation/provider/PREP reads and can report a
payload failure for work that cannot be admitted. Existing tests prove zero new
jobs but do not observe the repeated builder calls.

## Scope

In `_enqueue_candidate`, derive the existing dedup key once after its unchanged
admission identity. Use existing `queue.dedup_replay` before calling the payload
builder; return `False` when the exact slot is already consumed. Pass that same
key to existing enqueue. Both APIs already default to NEVER recycling.

Keep ADR-0855's source/phase/route key, broader live-successor guard, insert-time
conflict guard, transactions, role grants, cursors and attempts. Do not change
payload construction, queue semantics, applied SQL or public interfaces. This
restores the existing no-op without doing its redundant derivation; no new ADR
or architecture is needed. Other queue findings retain their current owners.

### Failure model

- Actors/deployments: reconciler with its existing database role and trusted
  retained source jobs on supported Linux targets; local proof uses x86_64/PG.
- Invariants/assets: exact finite admission slot, unchanged prior job/activation,
  opportunity after real phase/source/route changes, and safe concurrent enqueue.
- Accepted classes: the observational preflight can race with later insertion;
  it does not promise zero duplicate computation during that race. Queue failure
  classification and keyset revisit after concurrent row removal stay unchanged.
- Covered elsewhere: atomic conflict handling by the existing queue; authority
  and worker fences, source validation and cursor bounds by their existing tests.

## Success

The six declared repair operations build once for a first slot and do not build
again across three repeats after running-attempt exhaustion or terminal failed,
succeeded or canceled state. A new valid source/phase/route still gets its slot.
Concurrent sweeps still admit only one job; existing state/payload checks remain.

## Validation

- `focused-test`: extend the existing unchanged-phase matrix in
  `tests/reconciler/test_external_boot_repair_budget.py` with a spy that awaits
  the real builder. Old source is RED with four calls instead of one; new source
  is GREEN across six operations/four consumed states, with actual reconciler role.
- `focused-test`: existing new-request, phase/deadline/attempt/route, different-key
  live-suppressor, timezone and concurrent-sweep cases retain admission behavior.
- Restore a controlled omitted-preflight fault and observe the matrix fail,
  then restore exact source and rerun GREEN. Run caller repair-budget/fairness
  tests, `just lint`, whole-tree `just type`, hooks and independent code review.

Focused command: `uv run python -m pytest
tests/reconciler/test_external_boot_repair_budget.py
tests/reconciler/test_external_boot_repairs.py -q --tb=short`.
