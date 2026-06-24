# ADR 0235 — Per-Run console evidence (#761)

- **Status:** Accepted
- **Date:** 2026-06-24
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0226](0226-runs-get-console-ref.md) (the
  `runs.get refs.console` slot this ADR makes per-Run-immutable),
  [ADR-0049](0049-crash-capture-tiers.md) (console as a capture tier),
  [ADR-0005](0005-postgres-object-store-state.md) (write-before-commit artifact persistence),
  [ADR-0233](0233-live-attach-halted-early-boot-crash.md) (the boot-outcome results this console
  evidence rides on).
- **Issue:** [#761](https://github.com/randomparity/kdive/issues/761) (part of epic #764).
- **Spec:** [`../superpowers/specs/2026-06-24-per-run-console-evidence.md`](../superpowers/specs/2026-06-24-per-run-console-evidence.md).

## Context

The console artifact is keyed to the **System** and overwritten, so the canonical
"reproduce crash → fix → re-run to verify" loop loses its "before" evidence. Verified against
`main`:

- **Local.** The boot handler captures the console per boot but writes it under a System-keyed
  object (`local/systems/<sys>/console`) and upserts a single row, so a second boot of the same
  System overwrites the bytes an earlier Run's `evidence_artifact_id` still points at
  (`jobs/handlers/runs_boot.py` `_store_console_artifact`/`_upsert_console_artifact_row`). The
  earlier Run's `refs.console` silently resolves to the later boot's bytes — exact A/B evidence
  loss.
- **Remote.** Different shape, same symptom class. The console is streamed by a System-scoped
  `ConsoleCollector` into rotating S3 parts and assembled **only at teardown** (reconciler GC
  `reap_console_collectors` → `collector.finalize()`), written System-keyed
  (`remote-libvirt/systems/<sys>/console`). The shared boot handler's local-file capture returns
  `None` for remote (no local console log), so a remote Run's `evidence_artifact_id` is **never
  set** — remote runs have no per-Run `refs.console` at all today.

The boot handler is already provider-agnostic except for console capture, which it hardcodes to a
local-file read — silently a no-op for remote.

## Decision

A Run's console evidence is a **per-Run, immutable artifact**, captured through one
provider-neutral seam, with **no schema migration** (the `artifacts` table has no uniqueness
constraint; the object key carries the run id).

### 1. Per-Run object key, insert-per-Run

The console object key includes the run id: `<tenant>/systems/<sys>/console-<run>`. Distinct Runs
write distinct keys and distinct `artifacts` rows; a Run's row is never mutated by another Run's
boot. A re-boot of the *same* Run (a retried boot step) refreshes that Run's own row (keyed on its
own object key), so within-Run idempotency is preserved while cross-Run immutability is the
invariant.

### 2. A provider-neutral console-snapshot seam, run in the boot worker

The boot handler dispatches through one helper (`_capture_run_console`) at **all** capture sites:
when the provider runtime carries a `console_snapshotter` (remote), it calls
`ConsoleSnapshotter.snapshot(conn, system_id, run_id)` — which builds its own object store, so the
store is not a handler parameter — returning a `ConsoleSnapshot(id, object_key, data)` or `None`;
otherwise (local) the handler captures the worker-local console log itself, re-keyed per-Run. Both
return the captured `(artifact_id, redacted_bytes)`, and crash-signature detection
(`_expected_crash_matches`, `_generic_panic_matches`) runs on those bytes unchanged. The handler
records the returned `artifact_id` as the boot step's `evidence_artifact_id` for **both** providers,
at every capture site: expected-crash, crashed-halted-live, ready, and the best-effort boot-failure
path. The seam writes the artifact row on the boot handler's own `conn`, so the row and the step's
`evidence_artifact_id` commit atomically — no orphan row, no dangling id.

- **Local** (no `console_snapshotter` on the runtime) reads the local console log, redacts, and
  writes the per-Run artifact — the existing logic, re-keyed per-Run.
- **Remote** provides a `console_snapshotter` that **assembles the already-rotated S3 console
  parts** (`<tenant>/systems/<sys>/console-parts-<n>`, written by the collector) and writes an
  immutable `console-<run>` artifact, reusing the existing `RemoteConsolePartStore` assembly. It
  does **not** touch the live collector: the collector and its in-memory buffer live in the
  **reconciler** process while boot jobs run in the **worker** tier, so an in-process
  `collector.snapshot()` call across that boundary is impossible. Reading settled S3 parts needs
  no live collector, so the seam runs entirely in the worker. The snapshot is **cumulative**
  (every part captured so far), not a per-Run slice (deferred to #773); each Run's artifact is
  still distinct and immutable, which is the bar. Because the bytes are cumulative, the
  crash-signature gates can match a *prior* boot's panic line in the same System's history — but
  this is exactly the behavior local already has: the libvirt serial `<log>` file (`<sys>.log`) is
  never truncated per-Run, so local's gates have always read the System's cumulative console.
  Remote reaching the same shape is parity, not a new asymmetry; scoping the gates to one boot is
  the #773 refinement for both providers.

**Caveat (accepted, documented) — best-effort completeness.** The per-Run snapshot reads the S3
parts **as of boot completion**, and those parts are produced asynchronously by the
reconciler-resident collector, so the snapshot can trail the live stream by the collector's pump
latency. The collector flushes the panic tail **immediately** on the crash marker
(`_maybe_rotate` → `_flush_tail` on `_CRASH_MARKER`), which minimizes loss for the high-value crash
"before" evidence, but it is not a synchronous guarantee: a just-emitted line the collector has not
yet pumped may be absent. Each per-Run snapshot is **immutable once written**, so a prior Run's
bytes never change (the acceptance bar); the unchanged teardown `finalize()` artifact remains the
authoritative System-lifetime console. Tightening this to a synchronous per-Run capture is the
cross-process work tracked separately ([#773](https://github.com/randomparity/kdive/issues/773)).

### 2a. Within-Run idempotency

A re-boot of the same Run (a retried boot step) refreshes that Run's **own** row, keyed on its
per-Run object key, on both providers — never inserting a second row for one Run. Cross-Run
immutability and within-Run idempotency are the two invariants.

### 3. No migration; legacy rows left frozen

No new column and no uniqueness constraint change. Pre-existing System-keyed `console` rows are
left as-is: with the per-Run key in force they stop being overwritten, so they freeze at their
last-written bytes. Runs created before this change keep resolving to those frozen rows; Runs
created after resolve to immutable per-Run rows.

## Consequences

- Two Runs against one System retain distinct, independently-retrievable console artifacts; a
  prior Run's `refs.console` always resolves to that boot's bytes (local and remote).
- Remote runs gain a per-Run `refs.console` they never had — a behavior addition, not just a fix.
- The boot handler stops hardcoding local-file console capture; console capture is a provider seam,
  so the handler is genuinely provider-agnostic. The seam runs in the worker and writes on the
  worker's own connection, so the artifact + step commit atomically (no cross-process call, no
  separate-connection orphan window).
- A remote per-Run console is a **best-effort** snapshot of the S3 parts at boot completion (it may
  trail the reconciler collector's pump latency; the crash marker triggers an immediate flush that
  minimizes loss). The snapshot is immutable once written, and the unchanged teardown artifact is
  the authoritative complete record. The collector itself is unchanged, so no console-streaming
  behavior regresses.
- Storage grows: one console artifact per Run instead of one per System (plus the unchanged
  System-lifetime teardown console on remote). Retention is the concern of epic #771 #768
  (clear-on-close / TTL); this ADR does not add a sweeper.
- Files touched: `jobs/handlers/runs_boot.py` (seam call + per-Run key + run_id at all capture
  sites), the provider runtime `console_snapshotter` port + remote implementation reusing
  `RemoteConsolePartStore` assembly, plus unit tests per boundary and a two-Runs-one-System test
  per provider. The `ConsoleCollector` is **not** modified.
- Rollback is removing the edits; the new per-Run rows simply stop being written (no persisted
  state requires reversal).

## Considered & rejected

- **Local-only fix, remote as follow-up.** Rejected by decision: the issue's acceptance requires
  local and remote to behave identically, and remote runs having no per-Run console at all is its
  own gap worth closing now.
- **Call the live collector from the boot worker (`snapshot`/`finalize`).** Rejected as
  infeasible: the `ConsoleCollector` and its in-memory buffer are reconciler-resident
  (`build_reconciler_console_hosting`), and boot jobs run in the separate worker tier — the worker
  holds no handle to the live collector. Assembling the already-rotated S3 parts needs no live
  collector and runs in the worker. (`finalize()` would also drop the stream, breaking capture for
  later boots.)
- **Cross-process trigger via the reconciler** (worker marks the run; reconciler snapshots on its
  tick). Rejected: it decouples the snapshot from boot completion, making `refs.console`
  eventually-consistent and adding a record + tick handshake. Worker-side S3 assembly keeps the
  capture synchronous with the boot step and atomic on one connection.
- **Per-Run slicing of the remote console** (assemble only parts since this boot). Deferred to
  [#773](https://github.com/randomparity/kdive/issues/773): it needs a per-Run part-index mark;
  the cumulative snapshot already gives distinct immutable per-Run artifacts, which is the
  acceptance bar.
- **A new `run_id` column on `artifacts` + UNIQUE(owner, name).** Rejected as unnecessary: the
  object key carries the run id and the app-level upsert already keys on it, so no migration is
  needed.
- **Migrate/backfill legacy System-keyed console rows.** Rejected: they freeze harmlessly under
  the new key; rewriting historical evidence is neither needed nor desirable.
