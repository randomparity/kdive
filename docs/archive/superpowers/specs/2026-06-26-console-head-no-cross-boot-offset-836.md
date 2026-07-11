# Spec — Console head trimmed by a stale cross-boot byte offset (#836)

- **Issue:** [#836](https://github.com/randomparity/kdive/issues/836)
  (`area:control-retrieve`, `provider:local-libvirt`, `type:bug`, `status:needs-design`).
- **ADR:** [`../../adr/0258-local-console-no-cross-boot-offset.md`](../../adr/0258-local-console-no-cross-boot-offset.md)
  (refines [ADR-0241](../../adr/0241-per-run-console-slicing.md); supersedes its local byte-offset mechanism).

## Problem

On a long clean local-libvirt boot the captured console artifact began at `t=0.300051s`
instead of `t=0.000000` — the early-boot printk (`Dentry cache hash table entries`, the
`Command line:` echo) was missing. A short early-panic boot kept its full head. Early-boot
evidence is lost on exactly the healthy long boots a verify run needs it.

## Verified root cause

The console artifact is the libvirt serial `<log file=…>`, wired into the domain XML at
*define* time, so there is no kdive-level attach race
(`providers/local_libvirt/lifecycle/xml.py:83-84`).

The head is trimmed by the per-Run **byte-offset boot-window mark** (ADR-0241):

- The boot-window `mark` is the console-log size read *before* `booter.boot`
  (`jobs/handlers/runs_boot.py:_mark_boot_window` → `_console_log_size`). `boot()`
  power-cycles the *same* domain into the *same* per-System log path (`{system_id}.log`),
  so the mark is the size left by the prior provision/install boot (`mark > 0`).
- Capture stores `read_console_log(path, offset=mark)` (`runs_boot.py:_read_redacted_console`).
- The slice (`runtime_paths.py:read_console_log`): `if 0 < offset <= len(data): return
  data[offset:]` else the whole file.

ADR-0241 assumed the serial `<log>` is **append-only** across boots. It is not. libvirt's
`<log>` `append` attribute **defaults to `off`**, and since the 2017 libvirt fix *"qemu:
command: Truncate the chardev logging file even if append is not present"* QEMU/virtlogd
**truncates the serial log on every domain start** unless `append='on'` is set. (Verified
against `libvirt.org/formatdomain.html` and the libvirt commit; confirmed on the host's
libvirt 11.6 RNG schema.) So every power-cycle starts the log at byte 0 while the stale
`mark` still points at the *prior* boot's size:

- **Long clean boot** (`new_size >= mark`): `data[mark:]` drops the first `mark` bytes of
  *this* boot → head gone.
- **Short panic boot** (`new_size < mark`): the rotation guard returns the whole file → head
  preserved.

That asymmetry is the exact observed signature. ADR-0241 named this as an accepted residual
under a premise this evidence disproves.

## Decision (summary; full rationale in ADR-0258)

Because libvirt truncates the local serial `<log>` on each power-cycle, the per-System log
already holds **only the current boot**. The cross-boot byte offset is therefore both
unnecessary (there is no prior-boot content to exclude) and the direct cause of the dropped
head. The fix removes the local byte offset and captures the whole current log; ADR-0241's
no-cross-boot-bleed guarantee is preserved by truncation instead of a fragile offset.

1. **Make the truncate-on-start contract explicit.** Render the serial `<log>` with
   `append="off"` (`xml.py`). It is already the libvirt default; setting it explicitly
   documents the per-boot-truncation invariant the capture relies on and is robust to a
   future default change.
2. **Remove the local byte offset.** `read_console_log` reads the whole file (drop the
   `offset` parameter and the rotation guard, `providers/shared/runtime_paths.py`). The
   local capture path in `jobs/handlers/runs_boot.py` no longer threads a byte offset:
   `_console_log_size` is removed, `_mark_boot_window` returns `0` for the local (no
   snapshotter) provider, and `_read_redacted_console`/`_capture_console_artifact` drop the
   `offset` parameter.
3. **Remote is unchanged.** The remote-libvirt part-index mark
   (`ConsoleSnapshotter.mark_boot_window` → `snapshot(start_index=…)`) is a different
   mechanism (out-of-band collector, part granularity) and stays. The `mark` value is still
   threaded through the shared boot handler and consumed only by the remote snapshotter.

## Acceptance criteria

- The rendered local domain XML serial `<log>` carries `append="off"`.
- `read_console_log(path)` returns the whole file; it has no `offset` parameter and no
  rotation-guard branch.
- For the local provider (`console_snapshotter` unset) the boot-window mark is `0` and the
  captured console is the whole current per-System log — a long clean boot's head
  (`t=0.000000`, `Command line:`) is retained.
- ADR-0241's gate guarantee holds: a readiness-failing Run does not match a prior boot's
  `Kernel panic`. The panic gates (`_generic_panic_matches` / `_expected_crash_matched_line`)
  run only on the `READINESS_FAILURE` path, which is reached only after `booter.boot` started
  the domain (`create()` succeeded) and libvirt truncated the prior boot's bytes from the log,
  so the gate input is this boot only. A unit test models the truncated-per-boot log (only
  this boot's bytes present) and asserts the prior panic is absent from the gate input.
- Remote part-index slicing and its tests are unchanged.
- Error contract unchanged: a `PermissionError` reading the log is still a
  `CONFIGURATION_ERROR` with the worker-readability remediation; other `OSError` is an
  `INFRASTRUCTURE_FAILURE`; an absent log is empty bytes.

## Out of scope / accepted residuals

- **Best-effort evidence capture on an `INSTALL_FAILURE` before `create()`.** The boot
  handler's error path (`runs_boot.py` `except CategorizedError`) captures the console
  best-effort even when `boot()` raised before the domain started — e.g. `create()` failed
  after `destroy()`, so libvirt never re-opened (truncated) the log. The whole-file read then
  persists the prior boot's bytes as this run's evidence artifact, where the old offset would
  have yielded an empty slice. This is **evidence only** — the panic-matching gates run solely
  on the `READINESS_FAILURE` path (reached only after a successful `create()`/truncate), so it
  cannot cause a cross-boot mislabel; the run is already FAILED. Accepted as a low-impact
  residual rather than adding pre-`create` capture-suppression logic.
- **virtlogd rotation of a multi-MiB boot.** virtlogd rotates the live file at its
  configured `max_size` (default ~2 MiB); a boot whose console exceeds that rotates the head
  into `<sys>.log.1`, which `read_console_log` does not read. A normal kernel boot console is
  well under 2 MiB, so this is a pre-existing residual unrelated to the stale-offset defect
  this issue fixes. Not addressed here.
- **Per-Run on-disk log files.** The issue's "preferred" alternative (rewrite `<serial><log
  file=>` to `{system_id}-{run_id}.log` per boot) is rejected in ADR-0258: it requires a
  `Booter.boot` signature change across three providers, a readiness-probe path change, and
  on-disk log accumulation, for robustness against a libvirt behavior we already assert with
  `append="off"`.
