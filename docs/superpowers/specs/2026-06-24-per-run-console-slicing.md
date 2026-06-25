# Per-Run console slicing (one boot window)

- **Date:** 2026-06-24
- **ADR:** [ADR-0241](../../adr/0241-per-run-console-slicing.md)
- **Issue:** #773 (refinement of #761 / [ADR-0235](../../adr/0235-per-run-console-evidence.md), epic #764)

Scope each Run's captured console — the per-Run artifact **and** the crash-signature gate input —
to that Run's own boot window, on **both** providers. Today the capture is cumulative: it carries
the System's whole console history, so a Run that fails readiness can match a **prior** boot's
`Kernel panic` line and be mislabelled `crashed_halted_live` / `expected_crash_observed` while
citing stale evidence.

This is a refinement, not a regression fix: ADR-0235 deliberately shipped the cumulative snapshot
("distinct, immutable per-Run artifact" was the bar) and deferred slicing here.

## Current reality (verified against this branch)

- `jobs/handlers/runs_boot.py`:
  - `boot_handler` (worker tier) resolves `snapshotter = binding.runtime.console_snapshotter`
    (remote sets it; local leaves it `None`) and `system_id`, then calls
    `_run_boot_and_capture_outcome`, which calls `booter.boot(system_id)` and, on every outcome,
    `_capture_run_console(conn, system_id, run_id, secret_registry, artifact_store, snapshotter)`.
  - `_capture_run_console` dispatches: snapshotter set → `snapshotter.snapshot(conn, system_id,
    run_id)`; else → `_capture_console_artifact` → `_read_redacted_console` →
    `read_console_log(console_log_path(system_id))` (the **whole** local file, `path.read_bytes()`).
  - The gates `_expected_crash_matches` / `_generic_panic_matches` run on the **returned bytes** —
    so whatever the capture's scope is, the gate's scope is identical. Slicing the capture slices
    the gate for free.
  - Capture sites: ready (`~435`), expected-crash (`~413`), crashed-halted-live
    (`_record_crash_halted_live ~303`), and the best-effort boot-failure path (`boot_handler ~488`).
- **Local.** `read_console_log` is `path.read_bytes()` (no offset); the serial `<log>` file
  (`/var/lib/kdive/console/<sys>.log`) is append-only — `_prepare_console_log` only `touch()`es it,
  never truncates per boot. So the file's bytes-before-this-boot are exactly the prior history.
- **Remote.** `RemoteLibvirtConsoleSnapshotter.snapshot` → `RemoteConsolePartStore.assemble(system_id)`
  = concat of **all** `console-parts-<n>` for the System (`list_part_indices` returns every index,
  sorted). Parts are produced by the reconciler-resident `ConsoleCollector`: it rotates a numbered
  part on a 64 KiB size threshold (`DEFAULT_ROTATION_THRESHOLD`) or **immediately** on a
  `_CRASH_MARKER` match (`kernel panic | BUG: unable to handle | Oops: | Call Trace: | general
  protection fault`). `_take_index` numbers parts monotonically and resumes past existing indices
  on a collector restart (`max(existing) + 1`).
- The boot job runs in the **worker**; the collector + its in-memory buffer are
  **reconciler-resident**. The worker holds no handle to the live collector (ADR-0235 §rejected).

## Design

### The mark is a boot-handler-local value (no persistence, no collector change)

The entire boot window — read the mark, `booter.boot`, observe readiness/crash, capture the console
— executes inside one `boot_handler` invocation in the worker. The mark therefore lives as a
**local value in that invocation**, computed once before `booter.boot` and threaded into every
capture site. This resolves ADR-0235's open question ("mark in-memory on the collector vs.
persisted") with a third answer: **neither** — the worker owns the synchronous boot window, so the
mark needs no durable home and no cross-process handshake.

The mark is read **before** `booter.boot(system_id)` (the call that powers/reboots the guest and so
produces this boot's console). Bytes already present at that instant belong to prior activity.

Per provider the mark is an `int` with a provider-specific meaning:

- **Local — byte offset.** Mark = current size of `<sys>.log` (`0` if absent). Capture and gates
  read `read_console_log(path, offset=mark)` → only bytes appended after boot start. Byte-precise:
  the local serial log is written synchronously by libvirt/virtlogd to a single append-only file.
- **Remote — next part index.** Mark = `max(list_part_indices(system_id)) + 1` (or `0` if none) at
  boot start. `snapshot` assembles only parts with `index >= mark`. The mark is read from the **S3
  part index list**, not the collector's memory, so it is unaffected by a collector
  restart/reconnect (indices stay monotonic via `_take_index`).

The mark is read on the boot handler's connection-free path; the local size read and the remote
index list both run in a worker thread (blocking I/O), like the existing capture.

### Seam changes

- `ConsoleSnapshotter` port (`providers/ports/console.py`): add
  `async def mark_boot_window(self, system_id) -> int` and extend
  `snapshot(conn, system_id, run_id, start_index: int)`.
- `RemoteLibvirtConsoleSnapshotter`: `mark_boot_window` returns the next part index (off-thread);
  `snapshot` passes `start_index` to `assemble`.
- `RemoteConsolePartStore.assemble(system_id, start_index: int = 0)`: skip parts with
  `index < start_index`. Default `0` keeps the teardown `finalize()` assembly (whole history)
  unchanged.
- `read_console_log(path, offset: int = 0)`: `seek(offset)` then read to EOF; default `0` preserves
  every other caller. Same `FileNotFoundError → b""` / `PermissionError → CONFIGURATION_ERROR` /
  `OSError → INFRASTRUCTURE_FAILURE` handling. An `offset` past EOF yields `b""` (safe).
- `runs_boot.py`: `boot_handler` computes `mark = _mark_boot_window(system_id, snapshotter)` after
  resolving `snapshotter`/`system_id` and before the boot, passes it into
  `_run_boot_and_capture_outcome` and uses it in the boot-failure best-effort capture; the mark
  threads through `_capture_run_console` → `snapshotter.snapshot(..., start_index=mark)` (remote) /
  `_capture_console_artifact(..., offset=mark)` (local). `_mark_boot_window`: snapshotter set →
  `await snapshotter.mark_boot_window(system_id)`; else the local file size (off-thread, `0` if
  absent). The mark read is best-effort: any failure degrades to `0` (cumulative — today's
  behavior) and never fails the boot.

### Within-Run idempotency, immutability — unchanged

A re-boot of the same Run recomputes the mark fresh and refreshes that Run's own per-Run row
(keyed on its `console-<run>` object key); cross-Run immutability (ADR-0235) is untouched. The
object key is unchanged — slicing changes only the **bytes** written under it, not the key.

## Accepted caveats (documented)

1. **Remote part-granularity.** The first sliced part can carry a prior boot's trailing
   un-flushed-at-mark bytes (the collector's in-memory tail at the instant of the mark). But a
   prior boot's **panic** triggers the collector's immediate `_CRASH_MARKER` flush, so its panic
   lands in a part **below** the mark and is excluded. The gates run **only** on the
   readiness-failure path, so the residual non-panic prior tail they might see is benign. Local is
   byte-precise and has no such residue.
2. **Remote pump latency (pre-existing, ADR-0235).** The snapshot reads parts as of boot
   completion; a just-emitted line the collector has not pumped may be absent. Unchanged by this
   work.
3. **Remote ready-path completeness.** A short healthy boot whose bytes never crossed the 64 KiB
   threshold and never hit a crash flush yields an **empty slice** → no per-Run artifact for that
   ready boot (vs. a cumulative artifact before). A normal kernel boot emits well over 64 KiB, so
   this is the quiet-boot tail case; tightening it to a synchronous per-Run capture is the separate
   **synchronous-completeness** refinement also pointed at #773 (second issue comment), **out of
   scope** here. Local has no such gap (synchronous file read). The mark-read failure fallback to
   `0` likewise degrades to cumulative, never to a hard failure.

These keep local and remote **behaviorally identical at the outcome level** — both scope the gate
to the current boot window (ADR-0235's acceptance bar). The byte-offset (local) vs. part-index
(remote) mechanism is exactly what #773 prescribes; the precision difference is inherent to
remote's out-of-band collector and predates this change.

## Acceptance (from #773)

- A Run's per-Run console artifact and crash-gate input contain only this boot's window, on both
  providers.
- A readiness-failing Run does **not** match a prior boot's `Kernel panic` from the same System's
  history (the cumulative-buffer mislabel is closed) on both providers.
- Slicing lands for **both** providers or neither — no provider's gate is left cumulative while the
  other is scoped.
- No schema migration; the per-Run object key and ADR-0235 immutability/idempotency are unchanged.

## Out of scope

- **Synchronous per-Run completeness** for remote (flush/quiesce the collector at boot completion):
  the other refinement pointed at #773, tracked separately.
- Per-Run slicing of the System-lifetime **teardown** `finalize()` artifact — it stays cumulative
  by design (the authoritative whole-System record).
- Retention/GC of per-Run artifacts (epic #771 / #768).

## Considered & rejected

- **Truncate/rotate the local serial log per boot** (the issue's alternative for local). Rejected:
  destructive to the append-only System-lifetime log other readers and the teardown record rely on;
  a byte offset is non-destructive and equally precise.
- **Persist the mark (DB column / collector field).** Rejected as unnecessary: the worker owns the
  whole synchronous boot window, so an in-invocation local value suffices; persisting it would add
  a schema/handshake for no gain and reintroduce the cross-process coupling ADR-0235 avoided.
- **Byte-offset slicing for remote** (record assembled-byte length at boot start). Rejected: the
  worker cannot address a byte boundary inside an asynchronously-produced part, and it shares
  part-granularity's straddle without being simpler; the part index is the natural remote mark.
- **Fall back to the cumulative slice when the remote window is empty.** Rejected: it reintroduces
  prior-boot bytes into the gate input — the exact defect this issue closes — to paper over the
  ready-path quiet-boot gap, which belongs to the synchronous-completeness refinement.
