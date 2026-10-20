# 0576 — The worker owns the console-log inode across boots

## Status

Accepted (2026-08-21)

## Context

ADR-0258 pinned `append="off"` on the local serial `<log>` so virtlogd truncates the
per-System console file on every domain start, making the whole file a byte-exact
current-boot window that each capture reads without cross-boot offset arithmetic. That
mechanism silently assumed the truncated file stays readable by the worker. Under the shared
session-libvirt endpoint (ADR-0575, #1937) it does not: the operator-owned daemon's virtlogd
no longer truncates in place — libvirt 12 unlinks the existing log and recreates it as
`root:0600` (#1147 proof record). A fixed, non-root worker therefore loses read access to its
own System's boot and readiness evidence the moment any domain starts, and every readiness
probe, crash watch, and evidence capture reads an empty or unreadable log.

The current-boot-only contract itself is not in dispute: a readiness gate must never match a
prior boot's panic bytes, so whatever mechanism replaces per-boot truncation still has to
reset the window at every start. The constraint set is: no privileged readback path for the
worker, no per-worker daemons (the shared endpoint authority of #1937 is load-bearing), and
the fix must hold across every real start path — provision, install boot, customization boot,
power-on, retry, and abnormal re-starts.

## Decision

We will keep one worker-owned console inode per System and move per-start truncation from
virtlogd into the worker.

1. The domain XML renders `<serial><log append="on">` (`xml.py`). With append enabled,
   virtlogd opens the *existing* file `O_APPEND` instead of unlinking and recreating it, so
   the inode the worker created survives every boot.
2. Before every domain start, the worker prepares its console log through one seam
   (`storage._prepare_console_log`): create-if-absent mode `0644`, open `O_NOFOLLOW`,
   verify the opened identity — regular file, owned by this worker, exactly one link — then
   restore `0644` and truncate to zero. The truncate-before-create ordering (destroy first,
   then prepare, then create) keeps prior-boot bytes out of the new window.
3. Every start path calls the seam: provisioning before `defineXML`+`create`, the booter's
   power-cycle between destroy and create (install boot, run boot, retry), the customization
   boot before its transient `createXML`, control power-on before `create` (skipped when the
   domain is already running), and snapshot revert of an inactive domain.
4. An unsafe identity — a symlinked path, a foreign-owned replacement (the daemon-recreated
   `root:0600` file), or a hard-linked file — fails the start with a categorized error naming
   the path, rather than booting evidence the worker cannot trust or read.
5. Readers are unchanged: `read_console_log` still reads the whole file as this boot's
   byte-exact window; the remote part-index mechanism (ADR-0241/ADR-0429) stands.

### Amendment (2026-10-20): every truncating start records a per-start boot marker (#3254)

This is an amendment rather than a rewrite. Later evidence qualifies decision 2's truncate and
decision 3's claim that every start path calls the seam, and the record stays append-only.
Keeping one inode across boots removed the only per-boot signal that console rotation's
`boot_id` had. `dev:ino` no longer changes per boot, and mtime advances on every append. So the
`dev:ino:mtime` identity started a new console-part generation whenever a rotation pass saw new
output, including a crash's panic stream. That contradicts the per-boot signal that
[ADR-0273](0273-observe-rotating-console-parts.md) decision 4 requires.

Each truncating start now writes a fresh 128-bit nonce, after its truncate, to a sibling marker
`<system_id>.boot`. The write is atomic: an exclusive temp file is renamed into place. A failed
write fails the start with the same `PROVISIONING_FAILURE` identity error, naming the marker.
Two functions truncate:

- `storage._prepare_console_log`, the seam.
- `readiness.prepare_console_readiness_window`, which the external boot session and the system
  authority lane use in place of the seam.

Both call `storage.record_console_boot`. The reconciler's identity is `dev:ino:<nonce>`. It
reads the marker only when it is a regular file owned by the log's owner and holds exactly 32
lowercase hex characters; otherwise the nonce is empty. Ordinary growth keeps its generation. A
power-cycle that regrows past the prior cursor still starts a new one, and a stat failure still
degrades to the reset-forcing empty identity. The marker follows the log's lifecycle.

## Consequences

A fixed non-root worker under the shared session daemon reads its own console again: the
daemon appends to a worker-owned `0644` file instead of recreating it `root:0600`. The
byte-exact current-boot guarantee of ADR-0258 holds by worker-side truncation instead of
virtlogd's, so no offset machinery returns.

Truncation now happens between destroy and create rather than inside the start call. A
bounded race remains: bytes virtlogd still buffers after destroy could land after the
truncate and lead the next window. Destroy completion is synchronous with QEMU's chardev
close on supported hosts, so the window is negligible; if a host ever shows it, the failure
mode is extra leading bytes, which the panic classifiers do not anchor on.

An out-of-band start (`virsh start`) bypasses the seam and hands the daemon a missing file to
recreate as `root:0600`. The next kdive start rejects the foreign identity and names the fix
instead of booting against unreadable or misattributed evidence — fail-fast beats silent
misread. Operators who bypass kdive own that recovery.

virtlogd size rotation (`<sys>.log.1`) behaves as before and is unchanged residual.

Files touched: `lifecycle/xml.py`, `lifecycle/storage.py`, `lifecycle/install.py`,
`lifecycle/control.py`, `lifecycle/snapshot.py`,
`lifecycle/rootfs/customization_boot.py`, `providers/shared/runtime_paths.py` (docstring),
plus the deterministic and live-gated tests for each.

## Considered & rejected

- **Privileged readback** (run the worker as root, a setuid reader, or `sudo cat`). Rejected:
  grants filesystem authority unrelated to console capture and undoes the fixed unprivileged
  worker boundary (ADR-0555).
- **Per-worker session daemons** (`qemu:///session` per worker account, where session
  virtlogd writes worker-owned logs). Rejected: abandons the shared endpoint and its
  provider-group authority (#1937, ADR-0575), multiplies daemons per host, and fragments
  resource visibility.
- **Daemon-side permission config** (qemu.conf/virtlogd group ownership, umask, logrotate
  hooks). Rejected: the daemon creates the file mode `0600`, which strips group bits no
  matter the directory defaults; behavior is version-dependent and lives outside the repo's
  control, so the contract would rest on host configuration we cannot assert.
- **Post-boot privileged chown** of the recreated `root:0600` file. Rejected: still needs a
  privileged actor, and leaves a window — often the whole boot — during which the worker
  cannot read the readiness evidence it is polling for.
- **Drop file logging; stream the PTY via `virDomainOpenConsole`.** Rejected: rewrites the
  entire capture, readiness, rotation, and sidecar architecture to avoid one permission
  attribute, and requires a reader attached across every boot window.
