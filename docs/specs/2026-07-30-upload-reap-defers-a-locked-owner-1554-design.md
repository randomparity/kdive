# Historical verification — Design — the upload reap defers a locked owner instead of waiting for it (#1554)

Recorded 2026-07-30 for the then-current checkout. These are historical results; they do not
establish a passing result or supported procedure for the current release.

`test_a_locked_owner_does_not_stall_an_unrelated_owner_in_the_same_pass` held one owner's
`LockScope.RUN` across a real pooled database pass. The pass had to delete the unlocked owner's
object while preserving the locked owner's row; `asyncio.wait_for` detected blocking.
Sibling cases checked later reaping after unlock and observable deferral counts.

`test_reaps_multiple_abandoned_owners_counted` is the mutation control for `count == 1`: without a
holder the same two-owner shape returns 2, so a phase 1 that deferred unconditionally cannot pass.

**Mutation-verified.** Against the pre-fix blocking acquisition the three deferral tests fail with
`TimeoutError` and the clean-pass control passes; against the fix all five pass, along with the 34
existing reaper and race-guard tests.

Decision and executable owners:

- [0453-row-first-upload-reap.md](../adr/0453-row-first-upload-reap.md)
- [0502-a-write-lease-closes-the-orphan-sweep-delete-race.md](../adr/0502-a-write-lease-closes-the-orphan-sweep-delete-race.md)
- [0509-upload-reap-sweep-rechecks-under-the-owner-lock.md](../adr/0509-upload-reap-sweep-rechecks-under-the-owner-lock.md)
- [0510-the-upload-reap-defers-a-locked-owner-instead-of-waiting-for-it.md](../adr/0510-the-upload-reap-defers-a-locked-owner-instead-of-waiting-for-it.md)
- [test_upload_reaper.py](../../tests/reconciler/test_upload_reaper.py)
