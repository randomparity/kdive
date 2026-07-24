# Implementation plan — investigation-rootfs reclaim via a worker job (#1522)

- **Branch:** `fix/reclaim-via-worker-job-1522` (worktree `/home/dave/src/kdive-worktrees/fix/reclaim-via-worker-job-1522`)
- **Base:** `main`
- **Guardrails:** `just ci` (full recipe list in `justfile`; sub-recipes run individually in CI)
- **Spec:** [`../../specs/2026-07-24-rootfs-reclaim-worker-job-1522-design.md`](../../specs/2026-07-24-rootfs-reclaim-worker-job-1522-design.md)
- **ADR:** [`../../adr/0442-rootfs-reclaim-worker-job.md`](../../adr/0442-rootfs-reclaim-worker-job.md)
- **Pre-assigned:** ADR-0442, migration 0078

## Tasks

1. **Failing regression test first.** `tests/jobs/handlers/test_rootfs_reclaim.py::test_reclaim_completes_when_reconciler_user_cannot_unlink` — seeds a closed investigation with a committed rootfs row, a staged base, and a `torn_down` referencer; drives the handler; asserts base + object + row + marker all gone. Companion assertion that the reconciler sweep itself makes no store call and no FS access (AC-1, AC-2).
2. **Job kind + payload + migration.** `JobKind.RECLAIM_INVESTIGATION_ROOTFS`,
   `ReclaimInvestigationRootfsPayload`, `_PAYLOAD_BY_KIND` entry, `0078_reclaim_investigation_rootfs_job_kind.sql`.
3. **Handler module.** `src/kdive/jobs/handlers/artifacts/rootfs_reclaim.py` — move the gate +
   reclaim helpers out of `gc.py`, flip the order to file → object → row, add the `INVESTIGATION`
   lock, add the drain/marker/staging-dir tail, fail the job on a real fault.
4. **Registrar.** `jobs/assembly.py` registrar tuple entry.
5. **Reconciler sweeps.** `gc.py` — the two sweeps become DB-only enqueue scans on a stable
   `dedup_key` with `recycle_terminal`/`recycle_canceled`. Delete `rootfs_dir_accessible`.
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
