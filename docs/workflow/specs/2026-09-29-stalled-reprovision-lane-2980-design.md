# Reconciler settles a stalled `reprovisioning` System (#2980)

## Scope and authority

Campaign 60f31b4f6024, issue #2980, token `q2980-1a29972b`. Operator decisions (2026-09-29): a new
non-retryable `reprovision_incomplete` category in parity with ADR-0513's `restore_incomplete`, and
an `updated_at` settle window like `_TEARDOWN_SETTLE` before settling behind a canceled job.
Approved exclusions: auto-teardown of the settled `failed` System (operator; ADR-0441 and #2929),
and recycling the reprovision job (out of scope). The decision is a dated amendment to
[ADR-0435](../../adr/0435-reclaim-failed-provision-artifacts.md), which owns the reprovision and
teardown contract through its #2928 and #2979 amendments. ADR-0378 owns the restore lane, not
reprovision.

## Problem

`reprovisioning` has edges only to `ready` and `failed`, and only the reprovision handler takes
them. If the job dead-letters (`repair_abandoned_jobs`, or a handler raise before its `try`, such
as `binding_for_system`) or is canceled, the System stays `reprovisioning`. Since #2928,
`systems.teardown` refuses that state and `repair_orphaned_systems` skips it, so nothing can wind
the System down.

## Design

1. `repair_stalled_reprovisioning_systems(conn) -> int` in `reconciler/repairs/systems.py`. It
   selects `reprovisioning` Systems (`ORDER BY s.id LIMIT 100`) with no **blocking** `reprovision`
   job. A job matches on `j.kind = 'reprovision' AND j.payload->>'system_id' = s.id::text`, because
   the dedup key `{uid}:reprovision:{digest}` is per profile. A job blocks when:
   - it is `queued` or `running`; or
   - it is `canceled`, or `failed` with `error_category = 'lease_expired'`, and
     `j.updated_at > now() - _TEARDOWN_SETTLE` (15 minutes).
2. Per candidate, under `advisory_xact_lock(SYSTEM)`: re-run the same predicate for that one
   System, then `SYSTEMS.update_state(FAILED)`, stamp the category (item 4), and audit
   `reprovisioning->failed` with tool `systems.reprovision` under `SYSTEM_RECONCILER_PRINCIPAL`.
   One candidate's exception is logged and skipped, as `repair_stalled_tearing_down_systems` does.
3. Registered as catalog entry `stalled_reprovisioning_systems`, right after
   `stalled_restoring_systems`, which already runs after `abandoned_jobs`.
4. `_restore_limbo_category` becomes `_limbo_category(conn, system_id, kind, verdict)`. It keeps
   the ADR-0513 §1a rule for both lanes: record `verdict` only when the newest non-active job of
   `kind` has no category or `lease_expired`, and otherwise leave the column NULL. The reprovision
   lane passes `REPROVISION` and `REPROVISION_INCOMPLETE`.
5. `ErrorCategory.REPROVISION_INCOMPLETE = "reprovision_incomplete"`, set `False` in
   `RETRYABLE_BY_CATEGORY`. Migration `0165_reprovision_incomplete_category.sql` rebuilds the four
   CHECK constraints from 0086 with the new value. The served errors guide and its
   `docs/guide/errors.md` mirror gain a section.

### Why the window covers `lease_expired` but not other `failed` rows

The worker finalizes a job only after its handler returns, so a `failed` row that the worker wrote
means no handler is running. That row settles at once, as decision 2 intends. But
`repair_abandoned_jobs` writes `failed`/`lease_expired` when the lease lapses. A non-capture handler
is not cancelled when its heartbeat stops (`jobs/worker.py` `_dispatch`, `_heartbeat_loop`), so it
may still be rebuilding the disk. That is the same exposure as `jobs.cancel`, so it gets the same
window. The window starts at the dead-letter write (the `jobs_set_updated_at` trigger), which is at
least one lease (5 minutes) after the last heartbeat.

A late handler after the settle is state-safe: `_commit_reprovision_result` commits only from
`reprovisioning`, and `_record_system_failure` logs `IllegalTransition` on a `failed` System. Only
its provider side effect remains, and the window bounds it.

## Success

- A `reprovisioning` System with no blocking reprovision job (Design item 1) becomes `failed` in
  one pass, with one `reprovisioning->failed` audit row. This covers a System with no job, and one
  whose job is `failed` without `lease_expired`, at any age.
- The category is `reprovision_incomplete` when no job exists, or when the newest non-active job
  carries no category or `lease_expired`. It is NULL when that job carries any other category.
- A `canceled` or `lease_expired` job updated within 15 minutes leaves the System untouched. Past 15
  minutes, it settles.
- A `queued` or `running` job leaves the System untouched at any age.
- A blocking job that appears between candidate selection and the lock leaves the System untouched.
- `systems.teardown` on the settled System enqueues a teardown, and that teardown succeeds.
- `retryable` is false for `reprovision_incomplete`, and all four CHECK constraints admit it.

## Failure model

1. **Actors and deployments**
   - the reconciler loop under the `kdive_reconciler` role, which runs one pass at a time;
   - the job worker running `reprovision_handler`; an operator calling `jobs.cancel` or
     `systems.teardown`.
2. **Invariants and assets at stake**
   - no settle while a reprovision handler may still write the provider disk: an active job, or a
     canceled or lease-lapsed one inside the window;
   - `failed` is terminal; `reprovisioning -> failed` is a legal edge (`state.py`);
   - a category the job already recorded is not displaced (ADR-0513 §1a).
3. **Accepted failure classes**
   - a handler that outlives the window (at least 20 minutes after its last heartbeat) races a
     later teardown. Accepted with the same bound ADR-0634 gives `_TEARDOWN_SETTLE`.
   - `systems.reprovision` back to an earlier profile replays that profile's terminal job
     (`recycle=NEVER`) and strands the System. This lane now settles that System to `failed`,
     which is better than stranding it forever. Follow-up candidate; not fixed here.
4. **Covered elsewhere**
   - tearing the settled System down: operator `systems.teardown` (#2929); not automatic (ADR-0441);
   - re-running the reprovision: excluded (operator).

## Considered and rejected

- **Settle every `failed` row at once (decision 2 read literally).** judgment: a lease-lapsed
  handler can still be running (the `_dispatch` path above), which is the hazard the window exists for.
- **Window on every terminal row.** judgment: it delays the common worker-finalized dead-letter by
  15 minutes and protects nothing.
- **Return to `ready`.** judgment: the disk is half rebuilt, the same reason ADR-0378 gives for
  restore.
