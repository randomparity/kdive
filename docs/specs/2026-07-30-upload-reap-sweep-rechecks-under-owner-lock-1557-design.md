# Design — the upload reap's object sweep re-checks each key under the owner lock (#1557)

- **Issue:** [#1557](https://github.com/randomparity/kdive/issues/1557)
- **ADR:** [0509](../adr/0509-upload-reap-sweep-rechecks-under-the-owner-lock.md)
- **Date:** 2026-07-30

## Requirement

`repair_abandoned_uploads` is two-phase. `_claim_abandoned_prefix` commits the `upload_manifests`
row delete under the owner's advisory lock and returns the keys that deletion abandoned;
`_sweep_uncommitted_objects` then deletes that list holding no lock, taking no connection, and
re-reading nothing. Any object written under the owner prefix between the phase-1 commit and that
key's `store.delete` is destroyed.

Three writers reach the prefix: a re-mint (the documented recovery from a reap), a
`control.capture_traffic` retry after a rolled-back PUT, and the vmcore `put_stream` /
`finalize_capture` pair. Both owner lanes are exposed, `investigations` included, because phase 2
no longer holds the `INVESTIGATION` lock `complete_rootfs_upload` takes.

Acceptance criteria, from the issue:

1. An object written by a re-minted upload window is never deleted by an in-flight sweep of the
   previous window, on either owner lane.
2. A test exercises a re-mint landing between the row-delete commit and the sweep's delete of that
   key.
3. The guard is compatible with a concurrent/fanned-out sweep (#1554).

## Mechanism

Phase 2 adopts the shape ADR-0502 shipped for the orphan sweep's `_delete_if_still_reclaimable`:
per key, one top-level transaction that takes the owner's advisory lock **non-blocking**,
re-evaluates the owner fences against committed state, and calls `store.delete` inside that
transaction. `hold_write_lease` mints under the same lock and `capture_traffic` holds it across its
whole PUT-plus-insert, so mint-before-write and re-check-before-delete are totally ordered.

The fences are the three the orphan sweep already evaluates, extracted to
`reconciler/cleanup/upload_fences.py` as one SQL fragment so the two passes cannot drift:

- no committed `artifacts` row reaches the key;
- the owner holds no `upload_manifests` row at all (a re-mint creates one);
- the owner holds no write lease with a live holder (`LIVE_HOLDER_SQL`).

The reaper does **not** apply the orphan sweep's `orphan_grace + upload_ttl` store-mtime term
(ADR-0509 §3), so it issues no HEAD and the `UploadStore` port is unchanged. Every fence is
evaluated in Postgres `now()`.

A key has three outcomes: deleted, declined (lock held, or a fence protects it), failed (the store
refused). Only `failed` feeds the end-of-pass raise and the ADR-0453 §4 brake.

## Plan

1. `src/kdive/reconciler/cleanup/upload_fences.py` (new) — `UploadOrphanCandidate`,
   `_OWNER_FENCE_SQL`, `reclaimable_upload_keys` (moved from `upload_orphans.py`), and
   `owner_key_is_fenced` for the reaper.
2. `src/kdive/reconciler/cleanup/upload_orphans.py` — import the moved names; no behaviour change.
3. `src/kdive/reconciler/cleanup/uploads.py` — `_sweep_uncommitted_objects` gains `conn`,
   `owner_kind`, `owner_id`; per-key lock, re-check, delete; new `_SweepOutcome`; `ReapOutcome`
   gains `declined`.
4. `tests/reconciler/test_upload_reaper.py` — the race tests below.

## Test obligations

- **The required race test.** A re-mint commits between the phase-1 row delete and the sweep's
  delete of a re-minted key; the re-minted bytes survive. Driven through the public
  `repair_abandoned_uploads` with `_HookedStore`, whose `before_delete` fires on the `to_thread`
  worker and therefore observes only committed state. Mutation-verified: it must fail against the
  unguarded sweep.
- The same on the `investigations` lane.
- A key that gains an `artifacts` row after the claim is **not** deleted — the inverse of
  `test_a_key_that_gains_an_artifacts_row_after_the_claim_is_still_deleted`, which pinned the
  unguarded behaviour and is replaced.
- A key whose owner gains a live write lease after the claim is not deleted; a lease whose holder
  is not live does not protect it.
- A key whose owner lock is held by another transaction is skipped, counted as neither deleted nor
  failed, and does not trip the ADR-0453 §4 brake.
- The existing suite still holds: the row still commits before the first delete, a failing delete
  still reports, a wholly refused sweep still stops the pass.
