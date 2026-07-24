# Investigation-rootfs reclaim via a worker job (#1522)

- **Issue:** [#1522](https://github.com/randomparity/kdive/issues/1522) — P1 bug
- **ADR:** [ADR-0442](../adr/0442-rootfs-reclaim-worker-job.md)
- **Supersedes the execution model of:** [ADR-0441](../adr/0441-investigation-scoped-uploaded-rootfs.md) §6

## Problem

On a host-process local-libvirt deployment the worker runs as root and the reconciler as the
invoking user. The reconciler can `stat` the root-owned `/var/lib/kdive/rootfs-uploads/<inv>` (so
ADR-0441 §6's `rootfs_dir_accessible()` gate passes) but cannot `unlink` inside it. Because
`_reclaim_rootfs_checksum` orders object → staged file → row, the S3 object is deleted while the
multi-GiB SENSITIVE staged base and its `artifacts` row are retained forever,
`rootfs_cleanup_pending_at` never clears, and the sweep re-fails every 30 s. Both sweeps share the
helper, so the TTL backstop fails identically: there is no reclaim path at all.

## Requirements

- **R1** — The staged-base unlink is performed by the process that created it (the worker), so no
  cross-user permission gap can exist.
- **R2** — The reconciler performs no host-filesystem mutation. `rootfs_dir_accessible()` is
  removed, not strengthened.
- **R3** — Both sweeps (close-driven and TTL backstop) are fixed by the same change.
- **R4** — ADR-0441 §6's overlay-absence liveness gate (both conditions, with its referencer
  enumeration) is preserved and evaluated immediately before the unlink.
- **R5** — The "never drop the `artifacts` row while the SENSITIVE object or file survives"
  ordering contract is preserved.
- **R6** — A mid-reclaim fault leaves a state that is safe to be stuck in and converges on retry.
- **R7** — A stuck reclaim is loud and durable, not a repeating log line.
- **R8** — The sweep does not accumulate job rows every pass.
- **R9** — Marker clearing and "all checksums drained" survive the split across two processes,
  including a worker that dies mid-reclaim.

## Design

### Job kind and payload

`JobKind.RECLAIM_INVESTIGATION_ROOTFS = "reclaim_investigation_rootfs"`. Migration **0078**
drop-and-recreates `jobs_kind_check` with the new value appended, following
`0053_console_rotate_job_kind.sql` / `0072_capture_traffic_job_kind.sql`.

```python
class ReclaimInvestigationRootfsPayload(_PayloadBase):
    investigation_id: str          # UUID
    artifact_ids: list[str]        # the due rows the reconciler selected; UUIDs, non-empty
```

The kind is internal/platform: absent from `CONTRIBUTOR_CANCELABLE_JOB_KINDS` (so `jobs.cancel`
requires operator, fails closed) and absent from `OPT_IN_DESTRUCTIVE_JOB_KINDS`.

### Reconciler side — `reconciler/cleanup/gc.py`

`gc_investigation_uploaded_rootfs` → `sweep_investigation_rootfs_reclaim(conn, grace)`:
selects investigations whose `rootfs_cleanup_pending_at` is older than `grace` **and** that still
have at least one `owner_kind='investigations'`/`retention_class='rootfs'` row, and enqueues one
reclaim job per investigation carrying every such row's id. Returns the number of investigations
for which a job was ensured.

`gc_expired_investigation_rootfs` → `sweep_expired_investigation_rootfs_reclaim(conn, retention)`:
selects rows past `retention` whose investigation is `open`/`active`, groups them by investigation,
and enqueues one job per investigation carrying that investigation's past-retention row ids.

Both take no `store`, no `rootfs_dir`, and no `uploads_dir`. Enqueue uses the stable dedup key
`rootfs-reclaim:<investigation_id>` and the `SYSTEM_RECONCILER_PRINCIPAL` authorizing tuple with the
investigation's `project`. Admission is gated in the sweep rather than by `queue`'s
`recycle_terminal`: an in-flight job is left alone, a settled one holds its slot until
`ROOTFS_RECLAIM_RETRY_BACKOFF` (5 min) has passed, and past that it is deleted and re-inserted so
`created_at` is re-dated (`dequeue` orders by `created_at`, so an in-place recycle would let a
faulting background reclaim head-of-line-block interactive work). `max_attempts=1` — the sweep is
the retry loop.

An investigation whose marker is past grace but which has **no** rootfs rows left still gets a job,
carrying an empty due set: the handler's drain tail is the only path that reaps a crash-orphaned
`*.partial` and clears the marker, so short-circuiting it in the reconciler would strand the orphan
or reintroduce a reconciler-side filesystem write. The reconciler therefore never writes to
`investigations` at all — its only write is the enqueue.

`rootfs_dir_accessible`, `_overlay_pins_base`, `_references_token`, `rootfs_base_reclaimable`,
`_rootfs_token_from_key`, `_unlink_staged_base`, `_sweep_investigation_staging_dir`,
`_reclaim_rootfs_checksum`, `_reclaim_object_if_reclaimable`, and
`_investigation_has_rootfs_objects` move out of `gc.py` into the handler module.

### Worker side — `jobs/handlers/artifacts/rootfs_reclaim.py`

```
reclaim_investigation_rootfs_handler(conn, job, *, artifact_store):
    inv = UUID(payload.investigation_id)
    faults = 0
    reclaimed = 0
    for artifact_id in payload.artifact_ids:
        async with conn.transaction(), advisory_xact_lock(conn, INVESTIGATION, inv):
            row = select id, object_key from artifacts
                  where id = %s and owner_kind='investigations' and retention_class='rootfs'
                    and owner_id = %s
            if row is None:                       # already drained — idempotent no-op
                continue
            token = _rootfs_token_from_key(object_key)
            if not await rootfs_base_reclaimable(conn, inv, token):
                continue                          # pinned: expected, not an error
            try:
                unlink staged base                # ENOENT = success
                store.delete(object_key)          # 404 = success
            except (OSError, Exception):
                faults += 1; log warning; continue   # row kept, before the row delete
            DELETE FROM artifacts WHERE id = %s
        reclaimed += 1
    async with conn.transaction(), advisory_xact_lock(conn, INVESTIGATION, inv):
        if no rootfs rows remain for inv:
            _sweep_investigation_staging_dir(UPLOADS_DIR, inv)
            UPDATE investigations SET rootfs_cleanup_pending_at = NULL WHERE id = inv
    if faults:
        raise CategorizedError(..., category=INFRA_ERROR)   # loud + dead-lettered
    return f"{reclaimed}"
```

Reclaim order per checksum is **file → object → row** (ADR-0442 §4). The overlay probe reads
`ROOTFS_DIR`; the staged base and staging dir are under `UPLOADS_DIR`; both are the
`providers/shared/runtime_paths` constants the staging path writes through.

The handler is registered from `jobs/assembly.py` with the object store, alongside the other
artifact handlers.

### Reconciler wiring — `reconciler/loop.py`

`ReconcileConfig.rootfs_dir` / `rootfs_uploads_dir` are removed (unreachable after the split). The
two `_RepairCatalogEntry` metric names become `investigation_rootfs_reclaims_enqueued` and
`expired_investigation_rootfs_reclaims_enqueued`, with the matching `ReconcileReport` fields.

## Acceptance criteria

- **AC-1** — A reclaim whose staged base sits in a directory the *reconciler* cannot write still
  completes: the base, the object, the `artifacts` row, and the marker are all gone. (Regression
  test simulates the `PermissionError` rather than requiring root.)
- **AC-2** — The reconciler sweeps perform no filesystem access and no object-store call; they only
  read the DB and enqueue.
- **AC-3** — `rootfs_dir_accessible` no longer exists anywhere in `src/`.
- **AC-4** — A live bound System with an overlay file present pins its base: the handler reclaims
  nothing, keeps the row and the marker, and **succeeds**.
- **AC-5** — A System in a pre-overlay/re-materializing state pins the base even with no overlay
  file (condition (b) preserved).
- **AC-6** — A System in the same investigation referencing a *different* checksum, a `catalog`
  rootfs, or an unparseable profile does not pin this checksum.
- **AC-7** — An unlink fault leaves the object **and** the row intact (nothing half-reclaimed) and
  fails the job.
- **AC-8** — An object-delete fault leaves the row intact; the next attempt finds the staged file
  already absent (`ENOENT` = success) and completes.
- **AC-9** — A payload naming an already-deleted artifact id is a no-op, not a failure.
- **AC-10** — The marker clears only when no rootfs row remains for the investigation, and the
  staging dir is swept (stale `*.partial` unlinked, then `rmdir`) under the same condition —
  including for a job whose due set is empty.
- **AC-11** — A partially-pinned investigation keeps its marker and its staging dir, and an
  in-flight `<token>.<uuid>.partial` is not clobbered.
- **AC-12** — Both sweeps enqueue on the stable per-investigation dedup key: a second pass while a
  job is `queued`/`running` does not create a second row; a settled job holds its slot through the
  backoff (its `failed` record survives); past the backoff it is replaced by a fresh row with a
  later `created_at` and this pass's due set, and a `canceled` job does not wedge the slot.
- **AC-15** — The handler blocks on the `INVESTIGATION` lock while a bind holds it uncommitted, and
  then observes the newly-bound pre-overlay referencer as a pin. Verified to fail with the lock
  removed.
- **AC-16** — A worker that dies mid-reclaim leaves its job `running` and the slot held; the sweep
  re-issues only after `repair_abandoned_jobs` dead-letters the zombie.
- **AC-13** — The TTL sweep enqueues only past-retention rows, and only for `open`/`active`
  investigations.
- **AC-14** — Migration 0078 admits the new kind and the enum↔constraint tie holds.
