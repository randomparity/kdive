# 0010 — External-boot release credits recovery-store capacity before cleanup deletes the objects

## Status

Open
review-by: 2027-03-03

## Concern

ADR-0583 and the merged ADR-0584 adapter disagree about when a release may credit recovery-store
capacity back, and the shipped code follows ADR-0584.

ADR-0583 states the invariant plainly:

> Release deletes that row and credits its bytes exactly once, but only after every owned recovery
> and materialization object has been deleted and verified absent.

and, for the terminal case:

> A terminal row with `cleanup_complete=false` remains fully charged.

The merged ADR-0584 local adapter reverses that ordering. `LocalExternalBootAuthorityAdapter` lists
`RELEASE` in neither `_MUTATING_OPERATIONS`
(`src/kdive/providers/local_libvirt/external_boot_authority.py:53-61`) nor `_DELETING_OPERATIONS`
(`:65`), and its comment at `:49-52` states that release's provider effect "is exactly one
observation", because "ADR-0584 makes conflict resolution, release, and teardown each allocate
their own later generation before mutating". Deletion belongs to `cleanup`, under a later
generation, which the SQL ordering agrees with: the `release` branch of
`commit_external_boot_authority_result` requires no prior release row, and the `cleanup` branch
requires one (`src/kdive/db/schema/0122_external_boot_authority.sql:1319-1330`, `:1386-1420`).

**The consequence ADR-0583's ordering was protecting is capacity accounting, and it is not held.**
The release branch inserts the release row and then executes
`DELETE FROM public.external_boot_reservations WHERE activation_id = p_activation_id`
(`0122_external_boot_authority.sql:1558-1559`). Deleting that row credits `reserved_bytes` back to
the store. Under the ordering the code actually implements, the owned recovery and materialization
objects still exist at that moment — `cleanup` has not run. So between the release commit and the
cleanup commit the recovery store is under-charged by `reserved_bytes` while those bytes are still
occupied, and ADR-0583's bound — "the sum of retained reservations in one provider instance's
recovery store cannot exceed its `recovery_max_bytes`" — does not hold for that interval.

**The permanent case is the one that matters.** If the follow-on `cleanup` job never commits, the
credit is never re-charged and the bytes are never reclaimed: an unbounded store leak rather than a
transient accounting skew. That is not a hypothetical composition. ADR-0593's Consequences record
that an authority-marked job whose handler raises before it can produce a binding-matching
`ExternalBootAuthorityFailure` is never written to the `jobs` table at all
(`src/kdive/jobs/worker.py:505-517`), burns one attempt per lease lapse, and then wedges
permanently `running` once `attempt >= max_attempts` — because `repair_abandoned_jobs` is fenced
against marked payloads (`src/kdive/reconciler/repairs/jobs.py:42-49`) and both generic finalizers
are fenced (`0122_external_boot_authority.sql:304-315`). A wedged `cleanup` job is what the default
configuration **will** produce once #2204 enqueues marked jobs while
`ExternalBootHandlerPorts.acknowledger` is still unwired — so the permanent case is the expected
one from that point, not the rare one. It is not reachable today: after #2205 nothing in
production enqueues a marked job at all, so this record describes an ordering hazard for #2118 to
sequence, not current production behaviour.

## Why deferred

Neither half is #2205's to fix. #2205's charter is job payloads and lifecycle handlers; the
release/cleanup ordering is settled by ADR-0584 and implemented in a merged provider adapter, and
the capacity accounting lives in the recovery-store reservation path, which no part of #2205
touches. Re-charging on release, or moving the `DELETE` to the cleanup branch, is a change to
`commit_external_boot_authority_result` — a `SECURITY DEFINER` function #2205 is explicitly
forbidden to modify, and whose claim-side neighbour #2201 was careful not to disturb.

The record exists because the contradiction was resolved, before this record, inside a design
specification. A spec cannot reconcile two accepted ADRs; only a new decision or a recorded
departure can. This is the recorded departure, in the shape
`docs/debt/0006-external-boot-detach-departs-from-adr-0583.md` and
`docs/debt/0008-external-boot-release-job-scan-under-the-system-lock.md` already use for departures
from the same ADR.

## Non-regression boundary

- #2205 must not make the interval longer or the leak likelier. Its `release` handler performs no
  provider mutation and no deletion, matching the adapter; it does not defer, batch, or
  conditionally skip the follow-on `cleanup`.
- #2205's `release` handler must not assert that an object is absent when it has not established
  that. Its `_ReleaseEvidence.objects` is empty under every adapter that exists today, because no
  method on `ExternalBootPorts` reports per-object absence, and `enumeration_complete` is truthful
  only because the domain the handler can check is empty. A future handler that populates that list
  without a port that can answer would convert this accounting defect into a false attestation.
- No change may re-implement the `repair_abandoned_jobs` marker predicate #2201 installed, or
  reopen the generic-finalizer fence, as a way of shortening the permanent case.

## What would resolve it

One of:

1. Move the reservation `DELETE` (and therefore the credit) from the `release` branch to the
   `cleanup` branch of `commit_external_boot_authority_result`, so capacity is credited when the
   objects are actually gone. This restores ADR-0583's invariant against ADR-0584's ordering and
   needs a migration plus a re-read of the release/cleanup preconditions.
2. Keep the credit on release and amend ADR-0583 with a new decision that states the reservation is
   released on the release commit and that `recovery_max_bytes` is a bound on *charged* rather than
   *occupied* bytes, together with whatever sweep re-charges or reclaims an activation whose cleanup
   never commits.

Done when a test asserts the recovery store's charged bytes and its occupied objects agree at every
commit boundary of one full activate → release → cleanup sequence, and when an activation whose
cleanup never commits does not leave bytes permanently credited.

## Provenance

target: src/kdive/db/schema/0122_external_boot_authority.sql
target: src/kdive/providers/local_libvirt/external_boot_authority.py
target: docs/workflow/specs/2026-09-03-external-boot-job-handlers-design.md
Found by the `$gauntlet` design review on the #2205 branch on 2026-09-03 (finding F4,
`run_id: gauntlet-2205-i1-5e0ba7e510b328eb`), which reproduced both citations against the tree and
identified the capacity half the specification had left unnamed. The `docs/debt/` surface widening
and this record number were authorized by the campaign orchestrator on the same date.
tracker: #2118
