# Implementation plan — investigation-rootfs reclaim via a worker job (#1522)

- **Branch:** `fix/reclaim-via-worker-job-1522` (worktree `/home/dave/src/kdive-worktrees/fix/reclaim-via-worker-job-1522`)
- **Base:** `main`
- **Guardrails:** `just ci` (full recipe list in `justfile`; sub-recipes run individually in CI)
- **Spec:** [`../../specs/2026-07-24-rootfs-reclaim-worker-job-1522-design.md`](../../specs/2026-07-24-rootfs-reclaim-worker-job-1522-design.md)
- **ADR:** [`../../adr/0442-rootfs-reclaim-worker-job.md`](../../adr/0442-rootfs-reclaim-worker-job.md)
- **Pre-assigned:** ADR-0442, migration 0078

## Tasks

1. **Failing regression test first.** `tests/jobs/handlers/test_rootfs_reclaim.py::test_unlink_permission_fault_never_deletes_the_object_or_row` — reproduces #1522's EPERM by dropping the staging dir's write bit (skipped under root) and asserts nothing is half-reclaimed; plus `::test_reclaims_base_object_row_and_marker` for the path #1522 had no working version of (AC-1). The reconciler sweeps take no `store` and no directory arguments at all, which is AC-2 structurally.
2. **Job kind + payload + migration.** `JobKind.RECLAIM_INVESTIGATION_ROOTFS`,
   `ReclaimInvestigationRootfsPayload`, `_PAYLOAD_BY_KIND` entry, `0078_reclaim_investigation_rootfs_job_kind.sql`.
3. **Handler module.** `src/kdive/jobs/handlers/artifacts/rootfs_reclaim.py` — move the gate +
   reclaim helpers out of `gc.py`, flip the order to file → object → row, add the `INVESTIGATION`
   lock, add the drain/marker/staging-dir tail, fail the job on a real fault.
4. **Registrar.** `jobs/assembly.py` registrar tuple entry.
5. **Reconciler sweeps.** `gc.py` — the two sweeps become DB-only enqueue scans on a stable
   `dedup_key`. Admission is gated in the sweep, not by `queue`'s `recycle_terminal`: an in-flight
   job is left alone, a settled one holds its slot for `ROOTFS_RECLAIM_RETRY_BACKOFF`, and past
   that the row is deleted and re-inserted so `created_at` is re-dated (`dequeue` orders by
   `created_at`). `max_attempts=1`. Delete `rootfs_dir_accessible`.
6. **Loop wiring.** `reconciler/loop.py` — drop `rootfs_dir`/`rootfs_uploads_dir`, rename the two
   repair metrics to `*_reclaims_enqueued`.
7. **Migrate the existing tests.** `tests/reconciler/test_gc_investigation_rootfs.py` and
   `tests/reconciler/test_rootfs_reclaim_gate.py` — sweep-side tests become enqueue assertions;
   gate/reclaim tests move to the handler test module. Cover AC-4 … AC-14.
8. **Regenerate + guardrails.** `just docs-check`, `just cli-verbs-check` (both regenerate
   `docs/guide/reference/jobs.md` and `_generated_verbs.py`), then full `just ci`.

## Open decisions — none

All design questions are settled in ADR-0442 (order flip, lock, fault contract, dedup key,
drain rule, payload shape). Do not re-litigate the direction: the maintainer chose the worker-job
shape in the issue's `WORK:TRAJECTORY` comment.
