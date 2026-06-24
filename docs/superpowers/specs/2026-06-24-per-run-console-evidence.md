# Per-Run console evidence

- **Date:** 2026-06-24
- **ADR:** [ADR-0235](../../adr/0235-per-run-console-evidence.md)
- **Issue:** #761 (epic #764)

Make a Run's console evidence per-Run and immutable, on both providers, with no migration.

## Current reality (verified)

- `jobs/handlers/runs_boot.py:_run_boot_and_capture_outcome` (provider-agnostic) calls
  `_capture_console_artifact(conn, system_id, secret_registry, artifact_store)` in three places
  (expected-crash, crashed-halted-live, ready) and records `evidence_artifact_id` from the result.
- `_capture_console_artifact` → `_read_redacted_console(read_console_log(console_log_path(system_id)))`
  (local worker file) → `_store_console_artifact` (tenant `"local"`, System-keyed `name="console"`)
  → `_upsert_console_artifact_row` (pre-check `LIKE %/console`, INSERT once then refresh etag).
- Remote: console is streamed by a System-scoped `ConsoleCollector` into rotating S3 parts;
  `finalize()` (called only from `reconciler/cleanup/gc.py:reap_console_collectors` at teardown)
  assembles + writes the System-keyed console (tenant `"remote-libvirt"`). The local-file capture
  returns `None` for remote, so remote runs set no `evidence_artifact_id`.
- `artifacts` table: no UNIQUE on (owner_kind, owner_id, name) — only the `id` PK. App-level upsert.

## Plan

### Step 0 — confirmed topology (resolved in design review)

- The boot job runs in the **worker** tier; the `ConsoleCollector` + `CollectorRegistry` are
  **reconciler-resident** (`__main__.py:build_reconciler_console_hosting`,
  `reconciler/loop.py:console_registry`). The worker therefore **cannot** call the live collector;
  it assembles the already-rotated S3 parts instead.
- Remote parts live at `<tenant=remote-libvirt>/systems/<sys>/console-parts-<n>`
  (`providers/remote_libvirt/console/wiring.py` `_part_key`/`_parts_prefix`/`list_part_indices`/
  `read_part`); assembly = concat in index order. Parts are already redacted per-part.
- `runs_boot.boot_handler` receives `artifact_store: ObjectStore | None` (the shared object store)
  and a `conn`; both providers' artifacts live in one bucket keyed by tenant prefix, so the worker
  store reads the remote tenant's parts.
- `ProviderRuntime` (`providers/core/runtime.py`) is a dataclass of capability ports; add an
  optional `console_snapshotter: ConsoleSnapshotter | None = None` (remote sets it, local leaves
  it None).

### Step 1 — per-Run key (local), the high-value core

- Thread `run_id` into `_capture_console_artifact` / `_store_console_artifact` /
  `_upsert_console_artifact_row`.
- Object key name becomes `console-<run_id>` → `local/systems/<sys>/console-<run>`.
- Upsert pre-check keys on the per-Run object key (exact, not `LIKE %/console`), so distinct Runs
  INSERT distinct rows; a same-Run re-boot refreshes its own row.
- `_run_boot_and_capture_outcome` passes `run.id` at all three call sites.

### Step 2 — the provider-neutral seam (worker-side)

- Define a `ConsoleSnapshotter` port:
  `snapshot(conn, system_id, run_id) -> ConsoleSnapshot | None` returning
  `(id, object_key, redacted_bytes)` (bytes needed for crash-signature detection). The store is
  **not** a parameter — a snapshotter that needs one builds it itself (see Step 3), keeping the
  seam provider-neutral. The artifact row is written on the passed `conn` so it commits atomically
  with the boot step.
- Dispatch in the boot handler: if `runtime.console_snapshotter` is set (remote) call it; else
  (local) use the Step-1 local-file capture. Apply at **all four** capture sites —
  `_record_crash_halted_live` (~248), expected-crash (~301), ready (~327), and the best-effort
  boot-failure path (~376) — so remote crash consoles are captured, not just the happy path.
- A `None` return leaves `evidence_artifact_id` unset (unchanged graceful path); a snapshot failure
  is logged and never fails the boot (mirrors the current best-effort capture).

### Step 3 — remote snapshotter (worker-side S3 assembly, collector untouched)

- Remote `ConsoleSnapshotter.snapshot(conn, system_id, run_id)`:
  0. Build the object store lazily via `object_store_from_env()` (so `composition.build_runtime`
     stays buildable without S3 config, ADR-0076); a config failure raises and is absorbed by the
     handler's best-effort wrapper into `None`.
  1. Assemble the System's console parts (`<tenant>/systems/<sys>/console-parts-<n>`, index order)
     — reuse `RemoteConsolePartStore.assemble` (`list_part_indices`/`read_part`). Blocking S3 I/O
     via `asyncio.to_thread`. Empty parts → return `None`.
  2. `RemoteConsolePartStore.put_run_console(name=f"console-{run_id}")` → object key
     `<tenant>/systems/<sys>/console-<run>` (object only; no row).
  3. Upsert the `artifacts` row on the passed `conn`, keyed on the per-Run object key
     (insert-or-refresh-etag), so a same-Run re-boot refreshes its own row and distinct Runs get
     distinct rows. Return `(id, object_key, bytes)`.
- The bytes are already redacted per-part; no re-redaction.
- The `ConsoleCollector` and the teardown `write_console_artifact`/`finalize()` are **unchanged**.
- Wire the remote runtime to expose `console_snapshotter=RemoteLibvirtConsoleSnapshotter()` in
  `composition.py` — no store or collector handle passed; the snapshotter builds its store lazily.

### Step 4 — tests

- Local: two Runs against one System → two distinct console object keys + rows; the first Run's
  `evidence_artifact_id` still resolves to its own bytes after the second boot. Re-boot of one Run
  refreshes its own row (no second row).
- Remote: the snapshotter assembles seeded S3 parts into a `console-<run>` artifact and returns
  the bytes; two run ids over the same seeded parts produce two distinct rows/keys; a same-run
  re-snapshot refreshes its own row (no duplicate). The `ConsoleCollector` is not involved in the
  test. Empty parts → `None`.
- Crash-signature gates still fire on the seam's returned bytes (expected-crash + generic-panic).
- Boundary: seam returns None → no `evidence_artifact_id` (unchanged graceful path).

## Acceptance (from #761)

- Two Runs against one System retain distinct, independently-retrievable console artifacts.
- A prior Run's `refs.console` always resolves to that boot's bytes.
- Local and remote behave identically (both populate a per-Run `refs.console`).

## Out of scope

- Per-Run *slicing* of the remote console (cumulative snapshot is sufficient here).
- Retention/GC of the now-per-Run artifacts (epic #771 #768).
