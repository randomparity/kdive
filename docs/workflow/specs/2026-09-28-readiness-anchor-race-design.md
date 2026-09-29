# Periodic readiness waits out an in-flight anchor (#2899)

## Problem

`ExternalBootAuthorityService._anchor` fsyncs the journal record, then awaits the database head
advance. The periodic readiness check reads the heads, then loads each lane with no lane lock.
In that window the file is one record ahead (or a new lane has no head row), so the check raises
`journal: head-mismatch` or `inventory-mismatch` and the host exits mid-request.

## Scope

Token `q2899-c7298f2b`, operator-approved 2026-09-28. Exclusions: #2898's defects; two follow-up
candidates (a job left running, the stale version string); manual journal edits. This amends
[ADR-0584](../../adr/0584-provider-host-authority-fences-external-boot-mutations.md).

- **Service.** The `quiesce_anchors()` async context manager closes an anchor gate and waits until
  no `_anchor` call is in flight. It reopens the gate on every exit, cancellation included.
  `_anchor` waits for an open gate, then counts as in flight from the append through the head
  advance and any retraction. It is in `_anchor`, not on lane locks: idle lanes are popped from
  `_lanes`. Anchors on different lanes stay concurrent.
- **Host.** `JournalInventoryValidator` gains an optional `anchor_quiescence` hook.
  `run_authority_host` arms it with `quiesce_anchors` only after the startup check passes and the
  mutation service exists. After a head-mismatch or inventory-mismatch, an armed validator
  re-reads the heads and reloads the lanes once inside `quiesce_anchors()`. A second failure is
  raised unchanged. The startup check and `check_authority_host_once` never arm the hook. The
  periodic check never retracts. The retry runs inside `READINESS_CHECK_TIMEOUT_SECONDS`.
- **Cache.** The validator updates its cache only after every lane matches. A failed first pass
  leaves it unchanged, so the retry reloads every changed lane.

### Failure model

1. Actors and deployments: the authority host under systemd; worker requests on its event loop.
2. Invariants and assets: a real file/head divergence still refuses service; anchors are never
   lost or reordered; authority availability.
3. Accepted: anchors on every lane stall for one heads query and one validation, only after a
   failed first pass, bounded by the 20 s timeout. An anchor cancelled between append and advance
   leaves a real divergence; the check fails and startup reconciles it (#2793).
4. Covered elsewhere: System-authority journals (separate root, not validated here); startup
   reconciliation (ADR-0584, #2793 amendment).

## Success

1. A periodic check that overlaps an anchor between its append and its advance passes.
2. A mismatch that persists under quiescence raises the same component and reason.
3. A stalled anchor fails the check with `readiness: timeout` within the bound; the gate reopens.
4. The startup and standalone checks never enter quiescence.

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| Success 1 | focused-test | `test_host.py` race test, red on main; DB reproduction in `test_connected_authority_acceptance.py` |
| Success 2 | focused-test | `test_host.py`: wrong-digest head and extra lane fail after one retry |
| Success 3 | focused-test | `test_host.py`: a paused advance times out; the mutation then completes |
| Success 4 | focused-test | `test_host.py`: startup check unarmed, periodic check armed |
| ADR-0584 amendment | task-test-not-applicable | prose record; `just records` checks its shape |
