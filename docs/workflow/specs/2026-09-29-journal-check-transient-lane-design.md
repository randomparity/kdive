# Periodic journal check retries a torn or vanished lane (#2933)

## Problem

The periodic readiness check loads lanes in a worker thread while `_anchor` appends, advances,
and may retract on the event loop. An anchor can then show as a torn final line or a truncated
read (`journal: invalid-lane`) or as a lane unlinked between `_local_lanes`' `os.listdir` and its
`os.stat` (`journal: unsafe-tree`). Neither is in the #2899 retry set, so the host exits. The
mechanism and why widening the retry is safe are recorded in the ADR-0584 amendment of
2026-09-29 (#2933).

## Scope

Token `q2933-a334636f`; exclusions as operator-approved in that charter (startup and standalone
`check` unchanged; append atomicity out of scope; a persistent torn lane still refuses service).
This amends [ADR-0584](../../adr/0584-provider-host-authority-fences-external-boot-mutations.md).

- **Vanished entry.** `_local_lanes` raises a private `HostReadinessError` subclass,
  `_LaneVanished`, when the per-name `os.stat` fails with `FileNotFoundError`. It carries the
  unchanged `journal: unsafe-tree` component, reason, message, details, category, and exit
  code; only a logged traceback names the subclass. Every other stat error and every
  structural cause (type, owner, mode, name) still raises a plain `HostReadinessError`.
- **Retry set.** `validate_current` uses a new predicate, `_may_be_in_flight_anchor`:
  `_is_head_divergence(error)`, the journal reason `invalid-lane`, or a `_LaneVanished`.
  `_is_head_divergence` stays unchanged for the startup reconcile gate in
  `run_authority_host`. The retry itself is unchanged: once, inside `anchor_quiescence()`,
  with fresh heads and lanes, within `READINESS_CHECK_TIMEOUT_SECONDS`, and only when armed.
- **Not changed.** `service.py`, the journal, the startup check, `check_authority_host_once`,
  the validator cache rule (written only after every lane matches), and the per-pass cost:
  anchors stall only for a retry after a failed first pass.

## Failure model

1. Actors and deployments:
   - the external-boot authority host under systemd, periodic check armed after startup;
   - mutation requests on its event loop; the check's loads in `asyncio.to_thread`.
2. Invariants and assets:
   - a lane that is torn, foreign, or wrongly owned or moded at rest refuses service;
   - each cause reports the same `journal: <reason>` as before; a retried failure reports what
     the quiesced pass observes (the #2899 rule);
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
| Success 5 | focused-test | `test_host.py`: unarmed validator over a listed-but-absent lane |
| Startup unchanged (criterion 4) | focused-test | #2899's `test_periodic_check_waits_out_an_anchor_between_append_and_advance` pins startup unarmed; the startup gate keeps calling `_is_head_divergence` |
