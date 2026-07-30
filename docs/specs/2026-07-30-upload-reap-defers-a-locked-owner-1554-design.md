# Design — the upload reap defers a locked owner instead of waiting for it (#1554)

- **Issue:** [#1554](https://github.com/randomparity/kdive/issues/1554)
- **ADR:** [0510](../adr/0510-the-upload-reap-defers-a-locked-owner-instead-of-waiting-for-it.md)
- **Date:** 2026-07-30

## Requirement

`repair_abandoned_uploads` reaps past-deadline upload windows one owner at a time, serially, on one
pooled connection. Phase 1 (`_claim_abandoned_prefix`) opens with a **blocking**
`advisory_xact_lock` on the owner's scope. The chunked `complete_build` takes that same
`LockScope.RUN` before its reassembly and holds it to request end, so on a multi-GiB upload the
reaper parks inside phase 1 for the whole reassembly — and with it every remaining candidate in the
loop, plus every repair sequenced after the reap in `_run_repair_plan`, which is also serial and
holds its pooled connection across the pass. No `lock_timeout` bounds the wait.

Acceptance criteria, from the issue:

1. One slow finalize must not delay the reap of an unrelated owner's expired window.
2. The reaper's timeliness contract holds for every owner not itself contended.
3. Whichever direction is taken — bounded concurrency or skip-and-continue — a skipped owner must
   not be silently starved (ADR-0453's condition on a skip).

## Mechanism

Phase 1 switches to `try_advisory_xact_lock`, the non-blocking sibling ADR-0502 added and the orphan
sweep and (since ADR-0509) the reaper's own phase 2 already use. On refusal, phase 1 returns having
read and written nothing, and the loop moves to the next candidate.

The deferral is safe because it consumes nothing. The manifest row is untouched and still past its
deadline, which is precisely the predicate the candidate select uses, so the next pass — thirty
seconds later — re-derives the owner. This is the point on which ADR-0509 §Consequences reached the
opposite conclusion, keeping phase 1 blocking because "a reap that gave up on a contended owner
would never claim it — the manifest row is the pass's only record". The row is not consumed by a
deferral, so the premise does not hold; ADR-0510 supersedes that paragraph and a test drives both
passes to pin it.

Three owner outcomes replace two:

| outcome | `_Claim` | `ReapOutcome` | counted in return | feeds §3 raise / §4 brake |
| --- | --- | --- | --- | --- |
| claimed | `keys` non-`None` | `reaped=True` | yes | per the sweep's own counts |
| declined | `keys=None, deferred=False` | `reaped=False, deferred=False` | no | no |
| deferred | `keys=None, deferred=True` | `reaped=False, deferred=True` | no | no |

A deferral must stay out of ADR-0453 §4's brake specifically: feeding it in would let one
long-running finalize stop the pass claiming the rest of the backlog, which is worse than the defect
being fixed.

Starvation is made visible without per-owner state. The candidate select gains `now() - deadline AS
past_due` — computed by Postgres, never a Python clock, which does not share the session timezone —
and the pass logs one `WARNING` when any owner was deferred, carrying the deferred count, the
candidate count and the oldest deferred age. That age grows pass over pass exactly when an owner is
starving and appears once when a finalize merely overlapped a pass.

The sweep stays serial: fan-out is an independent throughput change, not part of removing the wait.

## Tests

In `tests/reconciler/test_upload_reaper.py`, over a real `AsyncConnectionPool` and a holder
connection that keeps `LockScope.RUN` for one owner across the pass:

- **`test_a_locked_owner_does_not_stall_an_unrelated_owner_in_the_same_pass`** — the acceptance
  test. Two expired owners, one locked; the pass must return 1 and delete the free owner's object
  while the locked owner's row survives. The pass is wrapped in `asyncio.wait_for`, which is the
  assertion: against the blocking acquisition the pass never returns, because the holder is released
  only after the pass is awaited. Candidate order is not pinned — the select has no `ORDER BY` — and
  need not be, since blocking hangs the pass from either position.
- **`test_a_deferred_owner_is_reaped_by_the_next_pass_once_its_lock_is_free`** — the deferral defers
  rather than drops: pass 1 under the holder returns 0 and leaves the row, pass 2 after release
  reaps it.
- **`test_reap_one_owner_reports_a_held_lock_as_deferred_not_declined`** — the two no-claim outcomes
  are distinguishable, against `test_reap_one_owner_declines_renewed_manifest` as its opposite.
- **`test_a_deferred_owner_is_reported_with_its_count_and_age`** and
  **`test_a_clean_pass_reports_no_deferral`** — the summary fires with its count and age, and an
  uncontended pass stays quiet, so the line is not trained away.

`test_reaps_multiple_abandoned_owners_counted` is the mutation control for `count == 1`: without a
holder the same two-owner shape returns 2, so a phase 1 that deferred unconditionally cannot pass.

**Mutation-verified.** Against the pre-fix blocking acquisition the three deferral tests fail with
`TimeoutError` and the clean-pass control passes; against the fix all five pass, along with the 34
existing reaper and race-guard tests.
