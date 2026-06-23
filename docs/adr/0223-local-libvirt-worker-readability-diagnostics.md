# ADR 0223 — Actionable diagnostics for non-root worker readability under qemu:///system

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-23
- **Deciders:** kdive maintainers
- **Builds on:** [ADR-0211](0211-local-libvirt-host-dump-capture.md) (the host-dump
  capture seam whose worker-readability precondition this makes self-explanatory),
  [ADR-0222](0222-ubuntu-build-fs-libguestfs-diagnostics.md) (the same detect-and-guide
  shape for the build-fs path).

## Context

Driving the M2.8 B6 live verification (#680) on a real KVM host surfaced a single
root cause that blocked the advertised local-libvirt lifecycle at **boot
confirmation** and at **`vmcore.fetch method=host_dump`** (#699). The kdive worker
runs as a non-root user, but under the default `qemu:///system` URI libvirt's
helper daemons write files the worker must read back as **root**:

1. **Console log unreadable (boot confirmation).** `virtlogd` (root, under
   `qemu:///system`) writes the System console log at
   `/var/lib/kdive/console/<sys>.log` mode `root:0600` on every domain start. The
   boot handler reads that log to detect boot-to-multiuser, via the raw
   `path.read_bytes()` in `read_console_log` (`providers/shared/runtime_paths.py`).
   A non-root worker gets `PermissionError`, which `read_console_log` maps to
   `INFRASTRUCTURE_FAILURE` — a transient, retry-implying category. Boot never
   reaches `succeeded`, which gates `debug.start_session` ("run has no successful
   boot"). The domain genuinely booted (a raw `gdb target remote` against the
   `-gdb` stub showed a live kernel); only the control-plane *confirmation* failed.

2. **Host-dump core unreadable (`vmcore.fetch`).** `virDomainCoreDumpWithFormat`
   under `qemu:///system` writes the dump file with the **QEMU/root** identity, not
   the worker's. The capture path (`LocalLibvirtRetrieve._capture_via_file`) then
   reads that spooled file to compute the build-id, checksum, and redacted dmesg,
   and gets `[Errno 13] Permission denied`. The exception escapes uncategorized and
   reaches the operator as a generic infrastructure failure. ADR-0211 already
   *documented* this worker-readability precondition; #699 hit it live.

Both are the same class of problem the build-fs ADR-0222 addressed: a **host
identity/permission misconfiguration**, not a kdive defect and not something kdive
can fix in code (reading a `0600` file the worker does not own requires the worker
to *be* root, run against worker-owned `qemu:///session`, or be granted group
access — all operator/deployment choices). What kdive *can* do is stop reporting it
as an opaque, retryable infrastructure failure.

The constraint bounding any fix is the same as ADR-0222: there is no KVM/libvirt in
CI, so whatever we add must be unit-testable without a live domain —
`read_console_log` and the capture path via injected/simulated `PermissionError`,
and `check-local-libvirt.sh` via PATH/`$EUID` and env overrides (the existing test
patterns). See `../design/m2.8-local-libvirt-service-parity.md`.

## Decision

Make the readability failure self-explanatory rather than attempt an in-code fix of
the host environment:

1. **Category split in `read_console_log`.** A `PermissionError` reading the console
   log becomes `CONFIGURATION_ERROR` carrying a `remediation` detail that names the
   three operator fixes (run the worker as root, point `KDIVE_LIBVIRT_URI` at
   `qemu:///session`, or grant the worker group read access to virtlogd's output).
   Every *other* `OSError` (EIO, ENOSPC, a stale mount) keeps the existing
   `INFRASTRUCTURE_FAILURE` — those can be transient and retry-worthy; a permission
   denial on a local file never heals on retry. The `operation` and `path` details
   are unchanged, so the boot handler's existing
   `details.get("operation") == "read_console_log"` re-raise still fires.

2. **Category remap in the host-dump read path.** `LocalLibvirtRetrieve._capture_via_file`
   wraps the spooled-core reads (build-id, checksum, redacted dmesg) so a
   `PermissionError` becomes `CONFIGURATION_ERROR` with the same `remediation`
   guidance, instead of escaping uncategorized. Only the host-dump seam produces a
   foreign-owned core (the kdump overlay harvest writes worker-owned files), so in
   practice only `method=host_dump` triggers it, but the remap is correct for any
   unreadable spooled core. The store/network I/O in the same method keeps its own
   categories — only the local-file `PermissionError` is remapped.

   The two messages share one `remediation` string constant
   (`providers/shared/runtime_paths.py`) so the console and host-dump guidance never
   drift.

3. **Advisory preflight in `check-local-libvirt.sh`.** When `KDIVE_LIBVIRT_URI`
   resolves to a `qemu:///system` URI **and** the invoking user is non-root
   (`$EUID != 0`), emit a non-failing advisory (a new `note_warn`, printed like
   `note_fail` but it does **not** set the failure flag) explaining that
   boot-confirmation and `host_dump` capture need the worker to read root-owned
   virtlogd/QEMU output, and naming the same three fixes. It is advisory, **not**
   `note_fail`, because a non-root worker under `qemu:///system` is a *working*
   configuration for the build and kdump-capture planes (libguestfs reads the
   overlay itself) — only boot-confirmation and host-dump are affected — so failing
   the preflight would wrongly reject the host the project's own walkthrough targets.
   `$EUID` (a bash builtin) keeps it stub-free; a `qemu:///session` URI or a root
   runner suppresses it.

4. **Docs.** Cross-link this ADR from the local-libvirt walkthrough's worker-identity
   guidance and from ADR-0211's host-dump precondition note.

## Consequences

- A first-run operator who hits the permission wall gets `configuration_error` with
  a named fix instead of `infrastructure_failure` (boot) or a raw `Permission
  denied` dump (host-dump). The advisory preflight warns *before* a run when the
  identity/URI combination predicts the wall, but the runtime remap — not the
  preflight — is the guarantee, since the console file does not exist until a domain
  boots and cannot be probed ahead of time.
- The error category for these two cases changes (`INFRASTRUCTURE_FAILURE` →
  `CONFIGURATION_ERROR` for the console read; uncategorized → `CONFIGURATION_ERROR`
  for the host-dump read). Callers that branch on category for these signatures
  (none today) would see the new value. Boot still *fails* on an unreadable console
  — only the category and message improve; there is no change to the boot
  success/failure decision.
- The remap keys on the Python `PermissionError` type (EACCES/EPERM), not on stderr
  string matching, so it cannot silently regress on a wording change — unlike the
  ADR-0222 build-fs signatures.
- The preflight gains one advisory line for the common non-root/`qemu:///system`
  development host. It is intentionally non-fatal so the existing healthy-host tests
  (and `setup-local-libvirt.sh`, which calls the preflight) keep their exit codes.
- No MCP-surface, port, schema, migration, or dependency change.

## Alternatives considered

- **Run the worker as root / document only.** The issue lists "run the worker as
  root" as the simplest operator fix, and it is the recommended deployment identity
  for `qemu:///system`. Rejected as the *whole* answer: it is an operator choice
  kdive cannot make, and until the host is reconfigured the failure still reaches
  the agent — so it must be reported honestly regardless. We document the fix *and*
  classify the failure; they are complementary, not alternatives.
- **A virsh-mediated console read fallback** (`virsh console` / a privileged tail).
  Rejected: `virsh console` attaches to the live console stream, it does not read
  the *historical* boot log virtlogd already captured to the file, so it cannot
  reconstruct the boot-to-multiuser evidence; and a privileged-helper/sudo path is a
  new security surface this issue does not warrant. libvirt exposes no API to read
  the historical log bytes — the file is the source of truth.
- **A new `KDIVE_CONSOLE_DIR` / `KDIVE_HOST_DUMP_STAGING` path knob.** Rejected: the
  blocker is file *ownership*, not directory location — virtlogd and QEMU write
  `root:0600`/root-owned regardless of which worker-owned directory the path points
  into, so a relocation knob fixes nothing and is a speculative feature.
- **Hard-fail the preflight on non-root + `qemu:///system`.** Rejected: that
  combination is a working configuration for the build and kdump planes; a hard fail
  would reject the development host the walkthrough is written for. Advisory is the
  correct severity.
- **Remap `PermissionError` only for `method=host_dump`.** Rejected: the remap lives
  at the shared file-read boundary; any unreadable spooled core is a configuration
  problem, and scoping it to one method would re-categorize identically-caused
  failures inconsistently.
