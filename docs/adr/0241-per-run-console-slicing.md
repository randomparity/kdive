# ADR 0241 — Per-Run console slicing (one boot window) (#773)

- **Status:** Accepted
- **Date:** 2026-06-24
- **Deciders:** kdive maintainers
- **Refines (does not supersede):** [ADR-0235](0235-per-run-console-evidence.md) (per-Run immutable
  console evidence; this ADR scopes its captured **content** to one boot window),
  [ADR-0233](0233-live-attach-halted-early-boot-crash.md) (the crash-signature gates this slicing
  scopes).
- **Issue:** [#773](https://github.com/randomparity/kdive/issues/773) (refinement of #761, epic #764).
- **Spec:** [`../superpowers/specs/2026-06-24-per-run-console-slicing.md`](../superpowers/specs/2026-06-24-per-run-console-slicing.md).

## Context

ADR-0235 made a Run's console evidence a per-Run, immutable artifact on both providers, but the
captured **content** stays **cumulative** — it carries the System's console from System start up to
and including this boot, not just this Run's boot-window bytes. This holds for both providers: the
local libvirt serial `<log>` (`<sys>.log`) is append-only (`_prepare_console_log` only `touch()`es
it), and the remote `RemoteConsolePartStore.assemble` concatenates every `console-parts-<n>` the
System has produced.

The cumulative bytes feed the crash-signature gates (`_generic_panic_matches` /
`_expected_crash_matches` in `jobs/handlers/runs_boot.py`), which match a panic line **anywhere** in
the buffer (verified: no recency/boundary guard). So a Run that fails readiness can match a **prior**
boot's `Kernel panic` and be mislabelled `crashed_halted_live` / `expected_crash_observed` while
citing stale console evidence. The gates run **only** on the readiness-failure path, which bounds the
blast radius. ADR-0235 shipped the cumulative snapshot deliberately ("distinct immutable per-Run
artifact" was the bar) and deferred this slicing to #773.

The open design question ADR-0235 recorded was *where a per-Run boundary mark would live* — in the
collector's memory or persisted — both assuming cross-process coordination between the worker (boot
jobs) and the reconciler (the live console collector).

## Decision

Scope each Run's captured console — the per-Run artifact **and** the gate input — to that Run's own
boot window on both providers, using a **boot-handler-local mark** read just before the boot. No
schema migration, no collector change, no cross-process handshake.

### 1. The mark is a worker-local value for the boot's duration

The whole boot window — read the mark, `booter.boot(system_id)`, observe readiness/crash, capture
the console — runs inside one `boot_handler` invocation in the worker. The mark is therefore a local
value in that invocation, read **once before `booter.boot`** (the call that produces this boot's
console) and threaded into every capture site. This answers ADR-0235's open question with a third
option — **neither** collector-memory nor persisted: the worker owns the synchronous boot window, so
the mark needs no durable home. The gates read the same sliced bytes the capture returns, so scoping
the capture scopes the gate.

### 2. Per-provider mark mechanism (the issue's prescription)

- **Local — byte offset.** Mark = current size of `<sys>.log` (`0` if absent). Capture and gates
  read `read_console_log(path, offset=mark)`, precise to the byte between virtlogd rotations. The
  `<serial><log file=…>` is virtlogd-managed and rotates at a host `max_size` (default ~2 MiB), so
  `read_console_log` carries a **rotation guard**: if the mark exceeds the file's current size
  (rotated/truncated since the mark) it ignores the stale offset and reads the whole current file —
  degrading to cumulative for that one capture rather than an empty slice. The guard is a size
  comparison, so it does not catch rotation-with-regrowth-past-the-mark (a narrow accepted residual
  that drops this boot's pre-offset bytes; it fails safe for mislabeling — a missed panic abandons
  to FAILED). Tracking file identity to make local byte-exact across rotation was rejected as
  disproportionate to remote's coarser part-granularity.
- **Remote — next part index.** Mark = `max(list_part_indices(system_id)) + 1` (or `0`) at boot
  start, read from the **S3 part-index list** (not the collector's memory). `snapshot` assembles
  only parts with `index >= mark`. Reading the mark from S3 keeps it stable across collector
  restarts/reconnects: `_take_index` already numbers parts monotonically past existing indices.

A new `ConsoleSnapshotter.mark_boot_window(system_id) -> int` returns the remote mark;
`snapshot(conn, system_id, run_id, start_index)` consumes it. `RemoteConsolePartStore.assemble`
gains a `start_index=0` default (the teardown `finalize()` assembly stays whole-history).
`read_console_log` gains an `offset=0` default (every other caller unchanged). The mark read is
best-effort: any failure degrades to `0` (cumulative — today's behavior), never failing the boot.

### 3. Immutability and idempotency unchanged

The per-Run object key (`<tenant>/systems/<sys>/console-<run>`) is unchanged; slicing changes only
the bytes written under it. A same-Run re-boot recomputes the mark and refreshes that Run's own row
(ADR-0235). No migration: no schema touched.

## Consequences

- A readiness-failing Run no longer matches a prior boot's panic from the same System's history —
  the cumulative-buffer mislabel is closed on both providers.
- Slicing lands for **both** providers (ADR-0235 acceptance: neither provider's gate is left
  cumulative while the other is scoped). Local is byte-precise; remote is part-granular and
  best-effort — the precision difference is inherent to remote's out-of-band collector and predates
  this change.
- **Remote part-granularity (accepted).** The first sliced part can carry a prior boot's
  un-flushed-at-mark tail, but a prior boot's panic triggers the collector's immediate
  `_CRASH_MARKER` flush, so the panic lands in a part **below** the mark and is excluded; the
  residual non-panic tail the gates might see is benign.
- **Remote pump latency (pre-existing, ADR-0235).** A just-emitted line the collector has not pumped
  may be absent; unchanged here. The same window is a residual race for the prior-panic exclusion: on
  a very fast reboot a prior boot's panic may still be in the collector's buffer at mark time and
  flush into a part `>= mark`, so the prior-panic exclusion is best-effort under normal collector
  liveness (byte-exact on local), closed fully by the synchronous-completeness refinement.
- **Remote ready-path completeness (accepted, deferred).** A short healthy boot whose bytes never
  cross the 64 KiB rotation threshold and never hit a crash flush yields an empty slice → no per-Run
  artifact for that ready boot (a normal kernel boot emits well over 64 KiB). Tightening this is the
  separate synchronous-completeness refinement also pointed at #773; out of scope. Local has no such
  gap.
- Files touched: `jobs/handlers/runs_boot.py` (mark read + thread through the four capture sites),
  `providers/ports/console.py` (`mark_boot_window` + `snapshot` start_index),
  `providers/remote_libvirt/console/snapshot.py` (mark + sliced assemble),
  `providers/remote_libvirt/console/wiring.py` (`assemble` start_index),
  `providers/shared/runtime_paths.py` (`read_console_log` offset), plus unit tests per boundary and a
  slicing test per provider. The `ConsoleCollector` is **not** modified.
- Rollback is removing the edits: the marks stop being read and capture reverts to cumulative; no
  persisted state to reverse.

## Considered & rejected

- **Truncate/rotate the local serial log per boot** (the issue's local alternative). Rejected:
  destructive to the append-only System-lifetime log the teardown record and other readers rely on;
  a byte offset is non-destructive and equally precise.
- **Persist the mark (DB column / collector field).** Rejected: the worker owns the whole
  synchronous boot window, so an in-invocation local value suffices; persisting adds a
  schema/handshake for no gain and reintroduces the cross-process coupling ADR-0235 avoided.
- **Byte-offset slicing for remote.** Rejected: the worker cannot address a byte boundary inside an
  asynchronously-produced part; it shares part-granularity's straddle without being simpler. The
  part index is the natural remote mark.
- **Fall back to the cumulative slice when the remote window is empty.** Rejected: it reintroduces
  prior-boot bytes into the gate input — the exact defect this issue closes — to paper over the
  ready-path quiet-boot gap, which belongs to the synchronous-completeness refinement.
- **A "smarter" recency-aware gate** instead of slicing. Rejected (issue guidance): the lever is the
  per-Run boot-window mark applied to both providers, not a heuristic on an unbounded buffer.
