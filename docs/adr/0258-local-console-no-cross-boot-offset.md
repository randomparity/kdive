# ADR 0258 — Local console captured whole; no cross-boot byte offset (#836)

- **Status:** Accepted
- **Date:** 2026-06-26
- **Deciders:** kdive maintainers
- **Refines (supersedes the local byte-offset of):**
  [ADR-0241](0241-per-run-console-slicing.md) — that ADR's remote part-index mechanism is
  unchanged; only its local `<sys>.log` byte-offset mark is superseded.
- **Issue:** [#836](https://github.com/randomparity/kdive/issues/836).
- **Spec:** [`../superpowers/specs/2026-06-26-console-head-no-cross-boot-offset-836.md`](../archive/superpowers/specs/2026-06-26-console-head-no-cross-boot-offset-836.md).

## Context

ADR-0241 scoped each local-libvirt Run's captured console to one boot window with a
**byte-offset mark**: the size of the per-System serial log (`<sys>.log`) read just before
`booter.boot`, then `read_console_log(offset=mark)` returning `data[mark:]`. Its premise was
that the serial `<log>` is **append-only** across boots, so the offset was needed to exclude a
prior boot's bytes (chiefly to stop a readiness-failing Run from matching a prior boot's
`Kernel panic` in the cumulative buffer).

That premise is wrong. libvirt's `<serial><log>` `append` attribute **defaults to `off`**, and
since the 2017 libvirt fix *"qemu: command: Truncate the chardev logging file even if append is
not present"* QEMU/virtlogd **truncates the serial log on every domain start** unless
`append='on'` is set (libvirt.org/formatdomain.html; confirmed on the host's libvirt 11.6).
`boot()` power-cycles the domain (`destroy` + `create`), so each boot opens the same per-System
log fresh at byte 0. The stale `mark` still points at the prior boot's size, so:

- a **long clean boot** (`new_size >= mark`) gets `data[mark:]` — the first `mark` bytes of
  *this* boot are dropped, losing the early-boot head (#836);
- a **short panic boot** (`new_size < mark`) hits the rotation guard and keeps the whole file.

The offset solves a problem that cannot occur (there is no prior-boot content in a
truncated-on-start log) while actively corrupting the common case.

## Decision

We will capture the **whole** current local serial log and remove the cross-boot byte offset.

1. Render the local serial `<log>` with `append="off"` (`xml.py`). This is already libvirt's
   default; we set it explicitly to document and pin the per-boot-truncation invariant the
   capture relies on, independent of any future libvirt default change.
2. `read_console_log(path)` reads the whole file. The `offset` parameter and the rotation
   guard are removed (`providers/shared/runtime_paths.py`).
3. The local boot-capture path threads no byte offset: `_console_log_size` is removed,
   `_mark_boot_window` returns `0` for the local (no-snapshotter) provider, and
   `_read_redacted_console` / `_capture_console_artifact` drop their `offset` parameter
   (`jobs/handlers/runs_boot.py`).
4. The remote-libvirt part-index mark (`ConsoleSnapshotter.mark_boot_window` →
   `snapshot(start_index=…)`) is unchanged; the shared boot handler still computes and threads
   `mark`, now consumed only by the remote snapshotter.

ADR-0241's guarantee — a readiness-failing Run does not match a prior boot's panic — is
preserved on local by libvirt's truncate-on-start: the prior boot's bytes are gone from the
log, so the gate input is this boot only, byte-exact, with no offset arithmetic.

## Consequences

- A long clean local boot retains its early-boot head (`t=0.000000`, `Command line:`,
  `Dentry cache hash table entries`). The dropped-head defect is closed.
- The local capture is simpler: one read of the whole file, no offset, no rotation guard, no
  pre-boot size stat. The fragile `data[mark:]` arithmetic and ADR-0241's
  "rotation-with-regrowth-past-the-mark" residual are both gone.
- `read_console_log` loses its `offset` parameter — a contained API change; its only callers
  are the local boot-capture path and the readiness probe, both in-tree.
- The capture's correctness now depends on the `<log>` being truncated per boot. We pin that
  with `append="off"` in the XML we own, so the dependency is explicit rather than implicit in
  a libvirt default.
- **Accepted residual — evidence capture on `INSTALL_FAILURE` before `create()`.** The boot
  handler captures the console best-effort on its error path; if `boot()` raised before the
  domain started (`create()` failed after `destroy()`, so libvirt never truncated the log) the
  whole-file read persists the prior boot's bytes as this run's evidence (the old offset gave
  an empty slice there). This is evidence only — the panic gates run solely on the
  `READINESS_FAILURE` path, reached only after a successful `create()`/truncate, so it cannot
  cause a cross-boot mislabel. Accepted rather than adding pre-`create` capture suppression.
- **Accepted residual — virtlogd rotation.** A boot whose console exceeds virtlogd's
  `max_size` (default ~2 MiB) rotates the head into `<sys>.log.1`, which `read_console_log`
  does not read. A normal kernel boot is well under 2 MiB; this pre-existing limitation is
  unrelated to the stale-offset defect and is not addressed here.
- Files touched: `providers/local_libvirt/lifecycle/xml.py` (`append="off"`),
  `providers/shared/runtime_paths.py` (`read_console_log` whole-file),
  `jobs/handlers/runs_boot.py` (drop the local offset), plus the unit tests for each.
- Rollback is reverting the edits; no schema, migration, or persisted state is involved.

## Alternatives considered

- **Per-Run on-disk log file** (issue's "preferred": rewrite `<serial><log file=>` to
  `{system_id}-{run_id}.log` per boot, capture whole). Rejected: it requires a `Booter.boot`
  signature change across local, remote, and fault-inject providers, a readiness-probe path
  change to read the per-Run file, and unbounded per-Run on-disk log accumulation needing
  cleanup — all to gain robustness against a libvirt behavior (`append='off'` truncate) we
  already assert in the XML we control. The issue's premise that "`boot()` already re-renders
  the domain XML" is also inaccurate: only `install()` redefines the domain; `boot()` only
  power-cycles.
- **Truncate the local log before each boot** (issue's "alternative"; ADR-0241's first
  rejected option). Rejected: virtlogd already truncates on start, so an explicit truncate is
  redundant, and truncating a file virtlogd holds open races its write offset (a non-append
  fd would write past the truncated end, leaving a sparse file).
- **Inode-identity reset detection degrading the slice to whole-file** (issue's "minimum
  mitigation"). Rejected: it keeps the offset machinery to detect a reset that is in fact the
  *invariant* (every boot resets), adding complexity to support a slice that should never be
  taken locally.
- **Keep `append='on'` (cumulative) with a corrected offset.** Rejected: cumulative + offset
  is exactly ADR-0241's fragile design (stale-mark and rotation-regrowth failure modes). A
  per-boot-truncated whole-file read is simpler and the gate guarantee holds by truncation.
