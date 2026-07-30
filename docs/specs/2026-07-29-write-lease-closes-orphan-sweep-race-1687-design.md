# Design — a write lease closes the upload orphan sweep's delete/PUT race (#1687)

- **Issue:** #1687 · **ADR:** [ADR-0502](../adr/0502-a-write-lease-closes-the-orphan-sweep-delete-race.md)
- **Branch:** `feat/capture-write-lease-1687` · **Base:** `main`
- **Guardrail:** `just ci` (also `just lint`, `just type`, `just test`, `just schema-guard` each on
  their own exit code; `KDIVE_REQUIRE_DOCKER=1` for the db/integration tests)

## Requirement

ADR-0455 §3's residual — an object PUT between the orphan sweep's per-key re-classify and its
`store.delete` is destroyed — is **closed**, not mitigated a third time. ADR-0497 fenced the *row*
side (`finalize_capture` heads before committing); the bytes still go. The reachable writer is
local-libvirt's server-side `put_stream` under `local/runs/`, which mints no upload window and holds
no lock (ADR-0244 releases `LockScope.RUN` before the capture).

Acceptance:

1. An in-flight capture write under `local/runs/` cannot be deleted by the sweep.
2. The fence is **not** `upload_manifests` and does not widen #1557.
3. The lease has a deadline-governed reap (the capture handler's `except Exception:` does not
   release it).
4. `upload_orphans.py`'s deliberate abort-on-a-real-bug property is preserved — no `except` is
   widened — and `tests/reconciler/test_upload_orphan_sweep_malformed_reply.py` passes unchanged.
5. Migration `0084_*.sql` applies cleanly; `just schema-guard` exits 0.
6. ADR-0502 records why the issue's `upload_manifests` proposal was rejected (three reasons).

## Mechanism

Two parts, and **both** are required — the fence alone is not a closure:

- **State:** `object_write_leases(owner_kind, owner_id, job_id, created_at)`, PK
  `(owner_kind, owner_id, job_id)`, `job_id → jobs(id) ON DELETE CASCADE`. A third `NOT EXISTS` in
  `_RECLAIMABLE_SQL` skips a key whose owner holds a lease with a **live** holder
  (`jobs.state = 'running' AND jobs.lease_expires_at > now()`).
- **Ordering:** the mint takes `advisory_xact_lock(RUN|INVESTIGATION, owner_id)`; the sweep's
  `_delete_if_still_reclaimable` re-classifies and deletes inside one transaction holding
  `pg_try_advisory_xact_lock` on the same scope. A mint therefore either precedes the classify
  (seen → skip) or blocks until after the delete (its write follows the delete). A contended lock
  skips the key, un-counted.

Liveness comes from the job lease the worker already heartbeats, so no new knob and no second clock
that can lapse under a long capture. `reap_stale_write_leases` collects holder-less leases for table
growth, not for exposure.

## Plan

1. `src/kdive/db/schema/0084_object_write_leases.sql` — the table + `job_id` index.
2. `src/kdive/artifacts/upload_manifest.py` — receive `_LOCK_SCOPES`, `lock_scope_for`,
   `UPLOAD_OWNER_KINDS` (moved, no shim), beside `UPLOAD_TENANT`/`UploadOwnerKind`.
3. `src/kdive/db/locks.py` — add `try_advisory_xact_lock`.
4. `src/kdive/artifacts/write_lease.py` — `hold_write_lease` / `release_write_lease` /
   `reap_stale_write_leases` (+ `LIVE_HOLDER_SQL` shared with the sweep's fence so the two cannot
   disagree about liveness).
5. `src/kdive/reconciler/cleanup/upload_orphans.py` — third fence term; lock the per-key
   re-classify + delete.
6. `src/kdive/reconciler/cleanup/uploads.py` + `loop.py` — import the moved symbols; register the
   reap in `_REPAIR_CATALOG`.
7. `src/kdive/jobs/handlers/artifacts/vmcore.py` — mint between `precheck_run` and
   `retriever.capture`; release inside `finalize_capture`'s transaction.
8. Tests: sweep fence + lock ordering + reap + handler ordering; keep the unleased-loss test and
   re-point its docstring.

## Test obligations

- The window is opened with a **real** lock/writer, not a mocked stand-in.
- Mutation-verify each new assertion: neutralize the fence, the lock, and the mint separately and
  confirm the intended test reddens for the intended reason.
- Check no existing `== 0` / "nothing deleted" assertion in `test_upload_orphan_sweep.py` gains a
  second sufficient cause from the new fence (none seed a lease, so none should).
- No `except` in `upload_orphans.py` widens.
