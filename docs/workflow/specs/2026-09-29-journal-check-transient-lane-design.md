# Periodic journal check retries a torn or vanished lane (#2933)

## Problem

The periodic readiness check loads every lane in a worker thread while `_anchor` appends,
advances, and may retract on the event loop, with no exclusion against readers. Two views of an
in-flight anchor fall outside the #2899 quiesced retry set (`head-mismatch`,
`inventory-mismatch`), so the host exits:

1. **Torn read.** `append` writes with a loop of `os.write`. A load that reads the final line
   before its newline raises `ValueError("authority journal has a partial final record")`, which
   `_restore_journal_inventory` maps to `journal: invalid-lane`. A load racing a retraction's
   `ftruncate` fails the same way.
2. **Vanished entry.** A refused first record is retracted by unlinking its lane. If the unlink
   lands between `_local_lanes`' `os.listdir` and its `os.stat`, the stat raises `ENOENT`, which
   today raises `journal: unsafe-tree`.

## Scope

Token `q2933-a334636f`; exclusions as operator-approved in that charter (startup and standalone
`check` unchanged; append atomicity out of scope; a persistent torn lane still refuses service).
This amends [ADR-0584](../../adr/0584-provider-host-authority-fences-external-boot-mutations.md).

- **Vanished entry.** `_local_lanes` raises a private `HostReadinessError` subclass,
  `_LaneVanished`, when the per-name `os.stat` fails with `FileNotFoundError`. It carries the
  unchanged `journal: unsafe-tree` component, reason, message, and details, so logs, the
  `check` output, and the error category do not change. Every other stat error and every
  structural cause (type, owner, mode, name) still raises a plain `HostReadinessError`.
- **Retry set.** `validate_current`'s predicate becomes `_may_be_in_flight_anchor`: the journal
  reasons `head-mismatch`, `inventory-mismatch`, and `invalid-lane`, or a `_LaneVanished`. The
  retry itself is unchanged: once, inside `anchor_quiescence()`, with fresh heads and lanes,
  within `READINESS_CHECK_TIMEOUT_SECONDS`, and only when the hook is armed.
- **Not changed.** `service.py`, the journal, the startup check, `check_authority_host_once`,
  the validator cache rule (written only after every lane matches), and the per-pass cost:
  anchors stall only for a retry after a failed first pass.

Why widening is now safe: under quiescence no anchor is between `append` and the end of its
advance or retraction, so every lane file is at rest. A torn or vanished view that reappears
then is not an anchor in progress but a real defect, and it refuses service as before. The
retry only adds one read; it never tolerates, repairs, or retracts a lane.

## Failure model

1. Actors and deployments:
   - the external-boot authority host under systemd, periodic check armed after startup;
   - mutation requests on its event loop; the check's loads in `asyncio.to_thread`.
2. Invariants and assets:
   - a lane that is torn, foreign, or wrongly owned or moded at rest refuses service;
   - the diagnostic contract `journal: <reason>` is unchanged for every failure;
   - authority availability; anchors are never lost, reordered, or retracted by the check.
3. Accepted:
   - a persistent invalid lane costs one extra quiesced read before the host exits (bounded by
     the readiness timeout);
   - a non-authority actor deleting a lane between listing and stat is retried once; on the
     retry its absence is an `inventory-mismatch` (or a pass if it had no head), so no fence
     weakens;
   - a crash mid-append still leaves a torn lane that startup refuses (append atomicity is an
     approved exclusion).
4. Covered elsewhere:
   - startup reconciliation of an unanchored tail: #2793 amendment;
   - the quiescence gate and its release on every exit: #2899 amendment and its tests.

## Success

1. A periodic check whose first load reads an anchor's partial final line passes after its
   quiesced retry, and the anchor's mutation completes.
2. A periodic check whose listing sees a lane that a refused anchor's retraction unlinks before
   the stat passes after its quiesced retry.
3. A lane torn at rest fails with `journal: invalid-lane` after exactly one quiesced retry.
4. A lane with a structural `unsafe-tree` cause (wrong mode) fails with `journal: unsafe-tree`
   with no retry.
5. A vanished entry is reported as `journal: unsafe-tree` when the validator is unarmed.

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| Success 1 | focused-test | `test_host.py` mid-append race, red on main with `journal: invalid-lane` |
| Success 2 | focused-test | `test_host.py` mid-retraction race, red on main with `journal: unsafe-tree` |
| Success 3, 4 | focused-test | `test_host.py`: persistent cases, heads read and quiescence entered counted |
| Success 5 | focused-test | `test_host.py`: unarmed validator over the same race |
| ADR-0584 amendment | task-test-not-applicable | prose record; no executable consumer reads its text; `just records` checks its shape |
