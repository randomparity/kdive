# Spec — Actionable diagnostics for non-root worker readability under qemu:///system

- **Issue:** [#699](https://github.com/randomparity/kdive/issues/699) (M2.8 B6 precondition)
- **ADR:** [ADR-0223](../adr/0223-local-libvirt-worker-readability-diagnostics.md)
- **Date:** 2026-06-23

## Problem

A non-root kdive worker under the default `qemu:///system` URI cannot read the
root-owned files libvirt's helper daemons produce, blocking the local-libvirt
lifecycle at two points (both seen live in the B6 #680 drive):

1. **Boot confirmation.** `virtlogd` (root) writes the System console log
   `/var/lib/kdive/console/<sys>.log` mode `root:0600`. The boot handler reads it via
   `read_console_log` (`src/kdive/providers/shared/runtime_paths.py`) to detect
   boot-to-multiuser. A non-root worker gets `PermissionError`, currently mapped to
   `INFRASTRUCTURE_FAILURE` (retry-implying). Boot never reaches `succeeded`, which
   gates `debug.start_session`.
2. **`vmcore.fetch method=host_dump`.** `virDomainCoreDumpWithFormat` writes the dump
   with the QEMU/root identity. `LocalLibvirtRetrieve._capture_via_file`
   (`src/kdive/providers/local_libvirt/retrieve.py`) then reads that spooled file for
   build-id, checksum, and redacted dmesg, and gets `[Errno 13] Permission denied`,
   which escapes uncategorized.

This is a host identity/permission misconfiguration, not a kdive defect. kdive cannot
read a `0600` file it does not own; the genuine fixes (run the worker as root, use
`qemu:///session`, or grant group read access) are operator/deployment choices. The
goal is to **report the failure honestly and early**, mirroring the ADR-0222 build-fs
detect-and-guide treatment.

## Out of scope

- Making a non-root worker actually read root-owned files (requires root /
  `qemu:///session` / group access — operator choices, already documented in the
  walkthrough).
- A `virsh console` / privileged-helper console fallback (streams live, not the
  historical boot log; new security surface).
- Any path-relocation knob (the blocker is ownership, not directory location).

## Behavior

### B1 — console read category split

`read_console_log(path)`:

- A missing file stays empty (`b""`), unchanged.
- A `PermissionError` → `CONFIGURATION_ERROR` with `details` carrying
  `operation="read_console_log"`, `path`, `error="PermissionError"`, and a new
  `remediation` string naming the three operator fixes.
- Any other `OSError` → `INFRASTRUCTURE_FAILURE` (unchanged), since a non-permission
  error (EIO, ENOSPC, stale mount) can be transient.

The boot handler's existing `details.get("operation") == "read_console_log"` re-raise
still fires (the detail key is preserved), so the boot job fails with the new, more
actionable category instead of the old one.

### B2 — host-dump read remap

`LocalLibvirtRetrieve._capture_via_file(system_id, method, core)` wraps the
spooled-core reads (build-id, `_put_stream`'s checksum, redacted dmesg) so a
`PermissionError` becomes `CONFIGURATION_ERROR` with the same `remediation` guidance,
keyed on the `system_id`. The `finally`-block spool cleanup is unchanged. Store/network
errors keep their own categories — only the local-file `PermissionError` is remapped.

The `remediation` string is a single shared constant in `runtime_paths.py` so the
console and host-dump messages never drift.

### B3 — preflight advisory

`scripts/check-local-libvirt.sh` gains a `note_warn` helper (prints `WARN:` + a fix
line to stderr, does **not** set `fail`). When `KDIVE_LIBVIRT_URI` (default
`qemu:///system`) resolves to a `qemu:///system` URI **and** `$EUID != 0`, it emits an
advisory that boot-confirmation and host_dump need the worker to read root-owned
virtlogd/QEMU output, naming the three fixes. A `qemu:///session` URI or a root runner
suppresses it. It is advisory because the combination still works for the build and
kdump-capture planes.

### B4 — docs

- Update the walkthrough's worker-identity section: the boot-confirmation failure is
  now `configuration_error` (was `infrastructure_failure`), cross-link ADR-0223, and
  note the new preflight advisory.
- Cross-link ADR-0223 from ADR-0211's worker-readability precondition note.

## Acceptance criteria

- `read_console_log` returns `CONFIGURATION_ERROR` (with `remediation`) on
  `PermissionError` and `INFRASTRUCTURE_FAILURE` on other `OSError`; missing-file still
  empty. (unit)
- `_capture_via_file` raises `CONFIGURATION_ERROR` (with `remediation`) when a core
  read raises `PermissionError`; the success path and store-error path are unchanged;
  the spool is still cleaned up. (unit)
- `check-local-libvirt.sh` prints the advisory and exits `0` for a non-root runner with
  the default/system URI; prints no advisory for `KDIVE_LIBVIRT_URI=qemu:///session`;
  the existing healthy-host exit codes are unchanged. (shell, via the existing
  PATH-stub harness)
- `just ci` green (lint, type, lint-shell, lint-workflows, check-mermaid, test).

## Testing constraint

No KVM/libvirt in CI. The live seams (`_real_host_dump_capture`,
`_real_readiness`) stay `live_vm`-gated; the remap logic is exercised by driving
`read_console_log` and `_capture_via_file` with injected/simulated `PermissionError`,
and the preflight via the existing `tests/scripts/test_check_local_libvirt.py`
PATH-stub + env-override harness.
