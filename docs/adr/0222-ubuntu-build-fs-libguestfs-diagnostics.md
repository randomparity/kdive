# ADR 0222 — Actionable diagnostics for Ubuntu build-fs libguestfs failures

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-23
- **Deciders:** kdive maintainers

## Context

Clean-room validating the local-libvirt walkthrough (#690) on a stock Ubuntu
24.04.4 host surfaced two host-environment failures that block
`python -m kdive build-fs` (#694). Both are libguestfs-stage failures and both
reach the operator today as an opaque `PROVISIONING_FAILURE` carrying the last
2 KB of raw tool stderr (`run_guestfs_tool`, `images/planes/_build_common.py`):

1. **Host kernel unreadable.** libguestfs builds its supermin appliance from the
   host kernel, which Debian/Ubuntu ship `root:0600`. A non-root worker cannot
   read `/boot/vmlinuz-*`, so `virt-builder` dies with
   `cp: cannot open '/boot/vmlinuz-…' for reading: Permission denied` →
   `supermin: … command failed` → `libguestfs: error: … supermin exited`. The
   host fix is `sudo chmod 0644 /boot/vmlinuz-*` (or `dpkg-statoverride`).

2. **Appliance network (passt) failure.** With the appliance fixed, the
   `--install` phase needs appliance networking via `passt` and fails with
   `libguestfs error: passt exited with status 1`. The proximate cause is an
   AppArmor denial on passt's run-dir socket; unloading the profile clears that,
   but a libguestfs↔passt version mismatch on 24.04 can still block it. This is a
   host packaging problem — `--no-network` is not a workaround because it defeats
   the `--install` the build depends on — so kdive can detect and guide, not fix.

The walkthrough (#690) already documents both as Debian/Ubuntu caveats, but the
operator only sees that prose *after* a bare stderr dump, and only the kernel
fix can be checked cheaply before the slow build runs. The constraint bounding
any fix: no KVM/libguestfs in CI, so whatever we add must be unit-testable
without an appliance — `run_guestfs_tool` via injected stderr, and
`check-local-libvirt.sh` via PATH stubs (the existing test patterns). See
`../design/m2.8-local-libvirt-service-parity.md`.

## Decision

We will make the two failures self-explanatory rather than attempt an in-code
fix of the host environment:

1. **Signature remap in `run_guestfs_tool`.** On a non-zero tool exit, before the
   existing generic `PROVISIONING_FAILURE`, match two libguestfs stderr
   signatures and raise a `CONFIGURATION_ERROR` naming the host fix:
   - kernel-unreadable (`Permission denied` reading a `/boot/vmlinuz` path, or a
     supermin "cannot read … kernel" line) → the `chmod 0644 /boot/vmlinuz-*`
     fix;
   - passt appliance-network (`passt exited with status …`) → the AppArmor-unload
     note plus the "build on a working-appliance host / stage a prebuilt qcow2"
     fallback.

   Any other non-zero exit keeps the existing generic `PROVISIONING_FAILURE` with
   truncated stderr — the change is strictly additive classification, and it
   lives in the shared helper so both the local- and remote-libvirt build planes
   benefit (the signatures are libguestfs-generic, not local-only). The matched
   errors carry a `remediation` detail key and keep the truncated `stderr`.

2. **Cheap preflight in `check-local-libvirt.sh`.** Add a host-kernel-readability
   probe: if `/boot/vmlinuz-$(uname -r)` exists it must be readable by the
   invoking user, else `note_fail` with the `chmod` fix. Distro- and user-neutral
   (`-r` passes for a root worker or a `0644` Fedora kernel; the probe is skipped
   when the path is absent so an unusual `/boot` layout does not false-fail). A
   `KDIVE_HOST_KERNEL` override makes it stub-testable, mirroring
   `KDIVE_KVM_NODE`. The passt failure is **not** preflighted — see Alternatives.

3. **Docs.** Cross-link this ADR from the walkthrough's existing caveat block and
   note that the preflight now catches the kernel-readability case in Step 2.

## Consequences

- A first-run operator on Ubuntu 24.04 gets an actionable next step instead of a
  raw `cp: … Permission denied` / `passt exited with status 1` dump, and the
  kernel case is caught by `just check-local-libvirt` before the slow build.
- The error category for these two cases changes from `PROVISIONING_FAILURE` to
  `CONFIGURATION_ERROR` (the host, not kdive, must change) — consistent with the
  existing `_ensure_workspace_writable` host-setup mapping. Callers that branch on
  category for these signatures (none today) would see the new value.
- New obligation: the stderr signatures are matched as substrings/regex, so a
  future libguestfs wording change could silently fall back to the generic
  message. The fallback is safe (no worse than today) and the match is covered by
  tests pinned to the exact strings from #694.
- The preflight adds one more `note_fail` that is build-fs/kdump-specific, not
  needed to boot a prestaged image — the same "expected, non-fatal for the core
  lifecycle" shape the existing `import guestfs, drgn` check already has; the
  walkthrough already frames preflight fails this way.
- No MCP-surface, port, schema, migration, or dependency change.

## Alternatives considered

- **Auto-mitigate the passt failure** (select an alternate libguestfs network
  backend, or fall back to `--no-network`). Rejected: `--no-network` cannot run
  the `--install` the rootfs needs, and backend selection is a host packaging
  concern kdive cannot reliably drive — a phantom fix that would fail differently.
- **Full appliance/passt smoke test in the preflight** (run `libguestfs-test-tool`
  or a throwaway `virt-builder --install`). Rejected: it builds an appliance and
  needs host networking — tens of seconds and non-deterministic — which breaks the
  fast, report-only, no-side-effect contract of `check-local-libvirt.sh`. The
  passt case is surfaced instead by the build-time error remap (it cannot be
  cheaply predicted), and the appliance smoke command is named in the fix hint.
- **Auto-`chmod` the host kernel from kdive.** Rejected: it needs root and mutates
  host state from a build path; preflight is report-only and the walkthrough owns
  the operator-run fix.
- **Remap only in the local plane.** Rejected: the signatures are libguestfs-wide;
  putting the classification in the shared `run_guestfs_tool` covers the remote
  build plane's ephemeral-host appliance for free with no extra code.
