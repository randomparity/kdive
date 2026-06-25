# Expire uploaded build artifacts: TTL + clear-on-close (#768)

- **Issue:** [#768](https://github.com/randomparity/kdive/issues/768) — *Expire uploaded build
  artifacts (TTL) and clear them on investigation close.*
- **ADR:** [ADR-0234](../../adr/0234-external-build-default-and-contributor-role.md) decision 4 (the
  accepted policy this spec implements). Part of the external-build-default epic
  [#771](https://github.com/randomparity/kdive/issues/771).
- **Migration:** 0048.

## Problem

With agent-driven external upload now the default build lane (ADR-0234), uploaded kernels
accumulate without bound. Today `retention_class` is only an S3-lifecycle label — no app-level
enforcement for build/kernel artifacts — and closing an investigation only flips a state flag with
no cleanup. The only DB-level TTL sweep is `gc_report_artifacts` (report artifacts, 7-day default).
Uploaded build inputs (kernel/vmlinux/initrd) therefore live forever.

## Goal

Give uploaded build artifacts an enforced lifetime by two independent mechanisms, both deferred and
idempotent reconciler sweeps modeled on `gc_report_artifacts`:

1. **Clear-on-close** — closing an investigation eventually frees the build artifacts its runs
   uploaded, after a grace period.
2. **TTL backstop** — uploaded build artifacts expire by age regardless of close, so a
   never-closed investigation does not accumulate forever.

Console and other crash evidence are explicitly **out of scope to delete**.

## Scope of deletion (the explicit, documented choice)

Both sweeps target the **same row set**: run-owned kernel-binary artifacts.

```
owner_kind = 'runs' AND retention_class = ANY({'build', 'kernel-build'})
```

Rationale, ground-truthed against the codebase:

- **`retention_class='build'`** — every agent `create_run_upload` (and chunked-upload reassembly)
  kernel/vmlinux/initrd lands here (`mcp/tools/catalog/artifacts/uploads.py`,
  `artifacts/reassembly.py`). This is exactly "uploaded build artifacts."
- **`retention_class='kernel-build'`** — an internally *built* run kernel (owner_kind='runs', e.g.
  `providers/fault_inject/build.py`). Same nature: a large, re-creatable kernel binary attached to a
  run, never evidence. Included so built kernels do not accumulate either.
- **Excluded — `retention_class='build-log'`** — a *failed build's* captured output
  (owner_kind='runs', REDACTED; ADR-0238/#770). This is build **evidence**, kept like console.
  Because it is run-owned, the `owner_kind` predicate alone would not exclude it; the
  retention-class allowlist is what protects it.
- **Excluded — `retention_class='console'` and `'vmcore'`** — crash evidence. Both are
  **system-owned** (`owner_kind='systems'`; per-Run console is stored as a System-owned
  `console-<run>` object, `jobs/handlers/runs_boot.py`). They are excluded twice over: by the
  `owner_kind='runs'` predicate and by the allowlist. This is the A/B evidence epic #764/#761
  preserves; ADR-0234 constraint (b) forbids deleting it here.
- **Excluded — system-owned uploads (`owner_kind='systems'`, `retention_class='build'`)** — operator
  base-image uploads via `create_system_upload` (operator-gated, ADR-0234 decision 3). A System
  outlives a Run and may back runs across several investigations, so deleting its artifacts because
  one investigation closed could destroy another open investigation's inputs. Out of scope for
  clear-on-close *and* the TTL.

> **Deviation from the issue body, called out deliberately.** The issue text says "delete … for all
> runs **(and run-attached systems)**." We deliberately narrow to **run-owned** artifacts. Including
> run-attached *systems* would reach system-owned console evidence and operator base images — both
> protected by ADR-0234 constraint (b) and the shared-System hazard above. The TTL backstop already
> covers genuinely leaked run uploads; system base-image lifecycle is a separate operator concern.

## Mechanism

### Migration 0048 — the cleanup marker

Add one nullable column to `investigations` and back-mark already-closed rows:

```sql
ALTER TABLE investigations ADD COLUMN cleanup_pending_at timestamptz;
UPDATE investigations SET cleanup_pending_at = updated_at WHERE state = 'closed';
```

- Additive, forward-only (ADR-0015). Open/active rows get `NULL` (not pending).
- `NULL` ⇒ not marked for cleanup; a timestamp ⇒ marked at that instant, grace measured from it.
- **Backfill:** investigations already `closed` at migration time are back-marked with their
  `updated_at` (which equals their close instant — `closed` is terminal and `link`/`set`/`unlink`
  refuse terminal rows, so a closed row's `updated_at` is frozen at close). Without this, historical
  closed investigations would never be swept by clear-on-close and would rely solely on the TTL
  backstop; the backfill gives every closed investigation the same close-driven cleanup.

Chosen over a per-artifact `expires_at` column (ADR-0234 rejected that for the close case): linkage
`artifact → run → investigation` already lets the reconciler find what a closed investigation owns,
and the TTL backstop needs no new artifact column (it sweeps the existing `artifacts.created_at`,
exactly like `gc_report_artifacts`).

### Close path — mark, never delete

`investigations._close_locked` sets `cleanup_pending_at = now()` **in the same transaction** as the
`open|active → closed` state flip. This is the only close-path change: a non-destructive marker, no
synchronous delete (ADR-0234 constraint (a) — close is `contributor`-gated, so it must not be a
destructive evidence operation). The existing close audit record stands; the marker rides the same
audited transition.

`closed` is terminal (`domain/capacity/state.py` — empty transition set) and `link`/`set`/`unlink`
refuse terminal investigations, so `cleanup_pending_at` is stamped exactly once (on the
open|active → closed transition) and never re-stamped by a tool. (The reconciler's marker-clear in
sweep 1 *does* write the row and bump `updated_at` via the `investigations_set_updated_at` trigger —
nothing keys on a closed investigation's `updated_at`, so this is inert, but it means "frozen
`updated_at`" holds only until the post-grace sweep clears the marker.)

### Reconciler sweep 1 — `gc_investigation_artifacts` (clear-on-close)

New repair in `reconciler/cleanup/gc.py`, signature mirroring `gc_report_artifacts`:

```
async def gc_investigation_artifacts(conn, store: ArtifactObjectDeleter, grace: timedelta) -> int
```

1. Select investigations where `cleanup_pending_at IS NOT NULL AND cleanup_pending_at < now() - grace`.
2. For each, select its run-owned build artifacts:
   `artifacts a JOIN runs r ON r.id = a.owner_id WHERE a.owner_kind='runs' AND a.retention_class = ANY(%s) AND r.investigation_id = <inv>`.
3. Delete each artifact object then row, per-object isolated (one store failure is logged and
   retried next pass, never aborts the sweep — same as `gc_report_artifacts`).
4. **Clear the marker** (`cleanup_pending_at = NULL`) for that investigation **only if every one of
   its build artifacts was deleted this pass**. A partial failure leaves the marker set so the next
   pass retries; a fully-drained investigation drops out of the worklist (bounded, no perpetual
   re-scan of all closed investigations).
5. Return the count of deleted artifacts.

Idempotent: re-running after a full drain finds the marker cleared (no work); re-running mid-grace
finds nothing past grace.

### Reconciler sweep 2 — `gc_expired_build_artifacts` (TTL backstop)

New repair in `reconciler/cleanup/gc.py`:

```
async def gc_expired_build_artifacts(conn, store: ArtifactObjectDeleter, retention: timedelta) -> int
```

Deletes the same run-owned build row set where `created_at < now() - retention`, regardless of
investigation state. Per-object isolation identical to `gc_report_artifacts`. No new column — sweeps
`artifacts.created_at`. The TTL is deliberately generous (default 30 days) because it can reap the
kernel of a **still-open** long-running investigation; it is a leak backstop, not the primary path.

#### Why reaping an in-use build artifact is safe (the load-bearing assumption)

Install **reads `kernel_ref` from the object store once** and stages it under
`{staging_root}/{system_id}/{run_id}/kernel` (`providers/local_libvirt/lifecycle/install.py`); the
libvirt domain's `<kernel>` element then points at that **staged host file**, so subsequent **boots
re-use the staged copy and never re-fetch S3**. The S3 object is therefore only re-read by a *fresh
install / reprovision*. Consequences of reaping a build artifact that is still referenced by a
`kernel_ref`:

- A later boot of an already-installed System: unaffected (uses the staged kernel).
- A fresh install/reprovision after the reap: the fetch returns a clean typed `STALE_HANDLE`
  (`store/objectstore.py` maps the S3 404 to `STALE_HANDLE`), surfaced as a normal install failure —
  **not** silent corruption or a partially-written guest. The kernel is reproducible; the agent
  re-builds and re-uploads (the default external-build loop).

This is why the deletion predicate intentionally does **not** gate on run/system state: a run-state
guard would never reap a never-closed investigation's artifacts (its runs stay non-terminal
indefinitely), defeating the backstop. The trade accepted here is "a >TTL-old, never-closed upload
may force one re-upload on a late reprovision," against "uploads accumulate forever." The grace-gated
close sweep is the primary, intentional path; the TTL is the abandoned-upload backstop.

### Wiring

- `reconciler/loop.py`: register both repairs in `_repair_plan` under the existing
  `config.upload_store is not None` branch (the same gate as `gc_report_artifacts`); add their names
  to `ALL_REPAIR_KINDS` and matching count fields on `ReconcileReport`
  (`investigation_artifacts_gc_count`, `expired_build_artifacts_gc_count`). The pinning test
  `test_all_repair_kinds_matches_a_fully_populated_plan` keeps the bound and the plan in lock-step.
- `config/core_settings.py`: two settings, mirroring `REPORT_ARTIFACT_RETENTION_DAYS`:
  - `KDIVE_INVESTIGATION_CLEANUP_GRACE_DAYS` (default `1`) — the close grace window.
  - `KDIVE_BUILD_ARTIFACT_RETENTION_DAYS` (default `30`) — the TTL backstop.
  Both in the `_STORE_USERS` process set.
- `reconciler/loop.py` `ReconcileConfig`: add `investigation_cleanup_grace` and
  `build_artifact_retention` timedeltas with module-default constants.
- `__main__.py`: plumb the two settings into `ReconcileConfig` next to `report_artifact_retention`.

## Acceptance

- Closing an investigation, then advancing past the grace window, frees its run-owned build
  artifacts (S3 object + `artifacts` row), idempotently; a console artifact (system-owned) under the
  same investigation's System is untouched.
- A build artifact older than the TTL is reaped even though its investigation never closed.
- A per-object store failure does not abort either sweep; the failed row is retried next pass.
- `build-log` (run-owned, evidence) and system-owned uploads are never reaped by either sweep.

## Test plan (TDD, against disposable Postgres + a recording fake store)

Mirror `tests/reconciler/test_gc_report_artifacts.py`:

- `gc_investigation_artifacts`:
  - deletes only run-owned `build`/`kernel-build` artifacts of a closed investigation past grace;
  - leaves them under grace; leaves an *open* investigation's untouched;
  - **leaves a System-owned `console` artifact** and a run-owned `build-log` artifact untouched;
  - clears the marker after a full drain (re-run deletes 0); leaves the marker on a per-object
    failure (failed row kept, marker retained);
  - idempotent on an already-swept investigation.
- `gc_expired_build_artifacts`:
  - deletes run-owned build artifacts past the TTL regardless of investigation state;
  - leaves fresh ones; leaves `console`/`build-log`/system-owned untouched;
  - per-object failure isolation.
- `_close_locked` sets `cleanup_pending_at`; a re-close (idempotent) does not move it.
- Migration 0048: column exists; an open row stays `NULL`; a pre-existing `closed` row is
  back-marked with its `updated_at` (the backfill).
- Loop wiring: `ALL_REPAIR_KINDS` equals the fully-populated plan's names; `ReconcileReport` carries
  the two new counts.

## Out of scope

- Reaping system-owned (operator base-image) uploads or any crash evidence.
- Any change to investigation reopen semantics (`closed` stays terminal).
- A per-artifact `expires_at` column.
