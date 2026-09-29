# Periodic readiness waits out an in-flight anchor (#2899)

## Problem

`ExternalBootAuthorityService._anchor` fsyncs the journal record, then awaits the database head
advance. The periodic readiness check reads the heads, then loads each lane with no lane lock.
Between them the file is one record ahead (or a new lane has no head row), so the check raises
`journal: head-mismatch` or `inventory-mismatch` and the host exits.

## Scope

Token `q2899-c7298f2b`; exclusions as operator-approved in that charter. This amends
[ADR-0584](../../adr/0584-provider-host-authority-fences-external-boot-mutations.md).

- **Service.** `quiesce_anchors()` closes an anchor gate, waits until no `_anchor` is in flight,
  and reopens the gate in a `finally`. `_anchor` waits for an open gate, then counts as in flight
  from the append through the advance and any retraction, released in a `finally` on every exit.
  Idle lanes leave `_lanes`, so the gate is in `_anchor`, not on lane locks.
- **Host.** `JournalInventoryValidator` gains an optional `anchor_quiescence` hook.
  `run_authority_host` arms it with `quiesce_anchors` only after the startup check passes and the
  mutation service exists. After a head-mismatch or inventory-mismatch, an armed validator
  re-reads the heads and reloads the lanes once inside `quiesce_anchors()`; a second failure is
  raised unchanged. Startup and `check_authority_host_once` never arm the hook. Nothing retracts.
  The retry runs inside `READINESS_CHECK_TIMEOUT_SECONDS`.
- **Cache.** Written only after every lane matches, so the retry reloads every changed lane.

### Failure model

1. Actors and deployments: the authority host under systemd; requests on its event loop.
2. Invariants and assets: a real file/head divergence still refuses service; anchors are never
   lost or reordered; authority availability.
3. Accepted: anchors on every lane stall for one heads query and one validation, only after a
   failed first pass, bounded by the 20 s timeout. An anchor cancelled between append and advance
   leaves a real divergence; the check fails and startup reconciles it (#2793). A load racing
   the append's write can read a partial record (`invalid-lane`); that reason is outside the
   approved retry set, so it still exits (follow-up candidate).
4. Covered elsewhere: System-authority journals (separate root); startup reconciliation (#2793).

## Success

1. A periodic check overlapping an anchor between its append and its advance passes.
2. A mismatch that persists under quiescence raises the same component and reason.
3. A stalled anchor fails the check with `readiness: timeout` within the bound; the gate reopens.
4. The startup and standalone checks never enter quiescence.

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| Success 1 | focused-test | `test_host.py` race test, red on main; DB-backed reproduction |
| Success 2 | focused-test | `test_host.py`: wrong-digest head and extra lane fail after one retry |
| Success 3 | focused-test | `test_host.py`: a paused advance times out; the mutation then completes |
| Gate release | focused-test | `test_host.py`: a raising advance releases its count; a failed retry leaves the cache unchanged and the gate open |
| Success 4 | focused-test | `test_host.py`: startup check unarmed, periodic check armed |
| ADR-0584 amendment | task-test-not-applicable | prose record; `just records` checks its shape |
