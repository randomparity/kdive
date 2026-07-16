# ADR 0364 — Firstboot customization verdict from a captured status, not a serial-console write

- **Status:** Accepted
- **Date:** 2026-07-15
- **Composes with:** [ADR-0345](0345-unified-customization-boot.md) (the boot-to-self-customize
  mechanism this refines — the firstboot script, its console marker, and the offline injector),
  [ADR-0351](0351-repack-ext4-older-guest-fsck-compat.md) and the other #1152 shared-mechanism
  fixes (this is the last EL9 customize-boot blocker after those)
- **Issue:** #1174 · Epic: #1139

## Context

ADR-0345 replaced `virt-customize --install` with a boot-to-self-customize path: the build
boots the rootfs's own kernel, a firstboot systemd oneshot runs a `/bin/sh` script that
installs the package set and self-removes, and the host reads a `kdive-customize-ok` /
`kdive-customize-failed` marker line off the guest serial console to decide the verdict.

The original firstboot script pointed the whole script's stdout/stderr straight at the serial
device (`exec > /dev/<console> 2>&1`) under `set -e`, with a `trap … EXIT` that echoed the fail
marker on any non-zero exit. The stated goal (#1147) was that a failed in-guest `dnf` must land
its error on the captured console, not vanish into the discarded guest journal.

That mechanism was proven end-to-end only on **Fedora**. No EL9 (CentOS Stream 9 / Rocky 9)
customize boot ever reached `kdive-customize-ok`. On Rocky 9 x86_64 under KVM the firstboot
exited right after the dnf metadata phase with **no install output and no error** on the
console — undiagnosed, tracked as this issue's Blocker 2.

Live diagnosis on local x86_64 KVM (proof record
`docs/design/2026-07-15-el9-customize-boot-1174-proof-record.md`) found the root cause, and it
is not the customization at all:

- `dnf` is a Python program. When it writes a large volume **directly to the serial tty**, the
  slow console cannot drain the output, and CPython's final stdout flush at interpreter shutdown
  fails — CPython then exits with code **120**. The dnf transaction itself *succeeded*.
- Under the script's `set -e`, that benign exit-120 tripped the `EXIT` trap and fired the fail
  marker, failing the build for a customization that had actually worked.
- The same serial-write unreliability discards the buffered dnf output, which is why the console
  showed neither the install output nor an error — the "silent" failure.

This was reproduced in isolation, decoupled from dnf: a trivial Python program writing a large
volume to `/dev/ttyS0` exits **120**; the identical program writing to a **file** or through a
**pipe** exits **0**. Even a plain `tee`/`cat` writing that volume to the serial tty can return
an error. The serial console is simply not a reliable sink for a command's exit-status path.

## Decision

**Derive the customization verdict from a captured exit status, never from a serial-console
write.** `render_firstboot_script` now emits:

```sh
#!/bin/sh
console=/dev/<console>
log=/run/kdive-customize.log
( set -e
  <exec steps…>
  rm -f <unit> <wants-symlink> <script>   # self-removal
) > "$log" 2>&1
rc=$?
cat "$log" > "$console" 2>/dev/null || true
sync
if [ "$rc" -eq 0 ]; then echo kdive-customize-ok > "$console"
else echo kdive-customize-failed > "$console"; fi
sync
systemctl poweroff
```

- The exec steps run in a `( set -e … )` **subshell** whose output is captured to a **plain
  file**. A file honours dnf's real exit code (no shutdown-flush 120) and `set -e` aborts the
  subshell on the first failing step. The subshell confines `set -e` so it cannot leak into the
  marker logic below.
- `rc=$?` is the **verdict**. The ok/failed marker is selected from `rc`, so a serial-write
  failure can never flip a good build to failed.
- The captured log is dumped to the console **best-effort** (`2>/dev/null || true`), preserving
  #1147's failure-evidence path: a failed step's error still reaches the host console log, it just
  no longer decides the outcome.
- `/run` is tmpfs, so the capture file never persists into the sealed, published image.
- The pre-marker `sync` is retained (ADR-0345 durability: the host force-destroys the domain the
  instant it reads the marker, so every customization write must be flushed first).

The `trap … EXIT` + `exec > /dev/<console>` design is removed entirely (no shim). The console
marker classifier and the offline injector are unchanged — the script still writes exactly one
`ok`/`failed` line to the console.

## Consequences

- EL9 (Rocky 9 / CentOS Stream 9) customize boots reach `kdive-customize-ok` on KVM x86_64; the
  composed-path proof is recorded in the proof record above.
- A genuinely *hung* (non-exiting) customization still self-reports no marker and surfaces as the
  host's `BOOT_TIMEOUT` (unchanged from ADR-0345). A step that exits non-zero still fires the
  fail marker promptly, and its error is still on the console.
- The full customization output is dumped to the console at the end rather than streamed live;
  the host polls the console on a 10 s cadence and reads the whole log after the dump, so this
  does not change what the host can observe on failure.

## Rejected alternatives

- **Keep `exec > /dev/<console>` and ignore exit 120.** 120 is not dnf-specific and could mask a
  real failure; suppressing it blindly trades a false-fail for a false-pass.
- **Route output through `tee`/process-substitution to the console.** `tee` (C) *also* returns a
  write error on the serial tty under load, and with `pipefail` that failed a build whose dnf
  succeeded (live-observed); `set -e` inside a process-substitution / `||`-context is additionally
  subject to the "ignored in an AND-OR list" gotcha, so it does not reliably stop on first
  failure. The file capture sidesteps both.
- **Quiet dnf (`-q`) to shrink the output.** Reduces but does not eliminate the serial-flush
  race, and still couples the verdict to a serial write.
