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
spooled-core reads so a `PermissionError` becomes `CONFIGURATION_ERROR` with the same
`remediation` guidance, keyed on the `system_id`. The `finally`-block spool cleanup is
unchanged. Store/network errors keep their own categories — only the local-file
`PermissionError` is remapped.

**Catch on `PermissionError` (the `OSError` subclass), at `_capture_via_file`** — not
on a stderr string. This is precise for three reasons confirmed against the seams:

- The **first** core read is `_read_vmcore_build_id_from_file(core)` (`retrieve.py:165`),
  which is `read_core_build_id_from_file` → `open_core_program` → drgn
  `prog.set_core_dump(os.fspath(core))` (`shared/debug_common/core_file.py:24-34`).
  drgn opens the file there, so an unreadable root-owned core raises a **raw
  `PermissionError`** — exactly the issue's `[Errno 13] Permission denied: '…/vmcore'`
  envelope. The build-id read fails first, before checksum or dmesg.
- `open_core_program`'s only wrapping is `ImportError → MISSING_DEPENDENCY` (drgn
  absent), which is a `CategorizedError`, **not** a `PermissionError` — so
  `except PermissionError` does not swallow the drgn-absent signal.
- `extract_dmesg_or_sentinel` (`retrieve_kdump.py:101-106`) degrades only
  `CategorizedError` (non-`MISSING_DEPENDENCY`) to a sentinel; a raw `PermissionError`
  is an `OSError`, not a `CategorizedError`, so it is not silently hidden there either —
  and it cannot be reached first anyway, since build-id reads before dmesg.

The TDD test drives `_capture_via_file` with the **build-id seam** raising
`PermissionError` (the real first-failure path) and asserts `CONFIGURATION_ERROR` +
`remediation` + spool cleanup; a separate test asserts a seam raising
`MISSING_DEPENDENCY` (drgn absent) is **not** remapped.

The `remediation` string is a single shared constant in `runtime_paths.py` so the
console and host-dump messages never drift.

### B3 — preflight advisory

`scripts/check-local-libvirt.sh` gains a `note_warn` helper (prints `WARN:` + a fix
line to stderr, does **not** set `fail`). The predicate is precise: the advisory fires
when the resolved URI is **exactly `qemu:///system`** (the literal local-system URI;
an unset/empty `KDIVE_LIBVIRT_URI` defaults to it) **and** `$EUID != 0`. A
`qemu:///session` URI, a root runner, or a transport-prefixed remote form
(`qemu+ssh://…/system`, `qemu+tcp://…`) all suppress it — the remote forms' root-owned
files live on a different host, so the local-runner identity is irrelevant there. When
it fires, the advisory explains that boot-confirmation and host_dump need the worker to
read root-owned virtlogd/QEMU output, naming the three fixes. It is advisory (not
`note_fail`) because the combination still works for the build and kdump-capture planes.

### B4 — docs

- Update the walkthrough's worker-identity section: the boot-confirmation failure is
  now `configuration_error` (was `infrastructure_failure`), cross-link ADR-0223, and
  note the new preflight advisory.
- Cross-link ADR-0223 from ADR-0211's worker-readability precondition note.

## Acceptance criteria

- `read_console_log` returns `CONFIGURATION_ERROR` (with `remediation`) on
  `PermissionError` and `INFRASTRUCTURE_FAILURE` on other `OSError`; missing-file still
  empty. (unit)
- `_capture_via_file` raises `CONFIGURATION_ERROR` (with `remediation`) when the
  build-id read seam (the first core read) raises `PermissionError`; a seam raising
  `MISSING_DEPENDENCY` (drgn absent) is **not** remapped; the success path and
  store-error path are unchanged; the spool is still cleaned up. (unit)
- `check-local-libvirt.sh` prints the advisory and exits `0` for a non-root runner with
  the default/system URI; prints **no** advisory for `KDIVE_LIBVIRT_URI=qemu:///session`
  or a `qemu+ssh://…/system` remote form; the existing healthy-host exit codes are
  unchanged. (shell, via the existing PATH-stub harness)
- `just ci` green (lint, type, lint-shell, lint-workflows, check-mermaid, test).

## Testing constraint

No KVM/libvirt in CI. The live seams (`_real_host_dump_capture`,
`_real_readiness`) stay `live_vm`-gated; the remap logic is exercised by driving
`read_console_log` and `_capture_via_file` with injected/simulated `PermissionError`,
and the preflight via the existing `tests/scripts/test_check_local_libvirt.py`
PATH-stub + env-override harness.
