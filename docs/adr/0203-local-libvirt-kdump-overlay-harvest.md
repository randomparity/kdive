# ADR 0203 — Local-libvirt Tier 3 kdump capture via host-side overlay harvest

- **Status:** Proposed
- **Date:** 2026-06-21
- **Deciders:** kdive maintainers

## Context

`CaptureMethod.KDUMP` is the production-fidelity capture tier (#115): the guest runs
`makedumpfile` under a `kexec` crash kernel and writes a filtered
`/var/crash/<timestamp>/vmcore` to its own root disk. The MCP surface, the crashkernel boot
cmdline (ADR-0030/0051, #116), the kdump-capable guest image catalog (ADR-0188, #598), and
a full remote-libvirt KDUMP capture (ADR-0084, guest agent + presigned upload) already
exist.

Local-libvirt does not. `LocalLibvirtRetrieve` advertises only
`{CONSOLE, HOST_DUMP, GDBSTUB}`, so `vmcore.fetch(method=kdump)` on a local System is
rejected at admission; the `_real_wait_for_vmcore` / `_real_extract_redacted` seams raise
`MISSING_DEPENDENCY`. The boot side is ready —
`LocalLibvirtProfilePolicy.capture_method` selects `KDUMP` from a profile `crashkernel`,
the cmdline gains `crashkernel=256M`, and the install preflight enforces a capture
initramfs — so a local System can boot kdump-capable but cannot have its core captured.

The defining fact for the harvest mechanism: a local-libvirt domain runs on the **same
host** as the kdive worker, and the guest's `/var/crash/vmcore` lands on the per-System
qcow2 overlay (`local_libvirt/lifecycle/storage.py::overlay_path`), a file the host already
owns. Remote-libvirt cannot assume this (its guest is across the network), which is why it
uses an in-guest agent and a presigned upload.

## Decision

Local-libvirt KDUMP capture harvests the guest-written vmcore **host-side, from the
System's own qcow2 overlay, via a read-only libguestfs mount** — no guest agent, no live
shared filesystem.

`LocalLibvirtRetrieve.capture` already dispatches `KDUMP` to the injected
`_wait_for_vmcore(system_id)` seam and turns its `None` into a `READINESS_FAILURE`. We
implement that seam (and `_real_extract_redacted`) and advertise the method:

1. **Force-off then read.** The seam opens the local libvirt connection, looks up the
   domain, and `destroy`s it (idempotent if already shut off) before reading the overlay.
   `vmcore.fetch` admits only on `SystemState.CRASHED`, so force-off matches the System's
   state and gives libguestfs a quiescent disk — libguestfs reads of a disk a running VM is
   writing are unsafe.
2. **Newest-core selection.** A read-only libguestfs mount lists `/var/crash/*/vmcore`
   (depth-bounded) and picks the newest by mtime, mirroring the in-guest helper
   (`find $CRASH_DIR -maxdepth 2 -name vmcore | sort -rn | head -n1`). No matching core →
   `None`. A core over the shared 5 GiB single-object ceiling → `CONFIGURATION_ERROR`.
3. **Reuse drgn extraction.** Build-id (VMCOREINFO `BUILD-ID`) and redacted dmesg come from
   the harvested core through the existing helpers shared with remote host_dump
   (`read_core_build_id_from_file`, `read_core_dmesg_from_file`), including the
   `DMESG_UNAVAILABLE` degrade path.
4. **Advertise `KDUMP`** in `supported_capture_methods` so admission accepts it.
5. **Seam split.** A `GuestCoreReader` protocol (`list_vmcores` / `read_vmcore`) plus the
   readiness loop, selection, and size cap are pure and unit-tested with a fake; only the
   real libguestfs mount/read, `domain.destroy()`, and drgn calls are
   `# pragma: no cover - live_vm`, selected by `from_env`.

No change to the MCP surface, the job/admission path, the boot/install plane, the guest
image catalog, the database schema, or any other provider.

## Consequences

- A profile that sets `crashkernel` now yields an end-to-end local Tier 3 path:
  boot-with-crashkernel → panic → `vmcore.fetch(method=kdump)` → `postmortem.crash`.
- `libguestfs` becomes a host prerequisite for local KDUMP capture (only); its absence is a
  typed `MISSING_DEPENDENCY`, mirroring how a missing `qemu-img` surfaces in
  `storage.py`. Documented in the local-libvirt runbook and host-prereq list.
- Harvesting force-stops the domain. For a `crashed` System this is benign — kdive does not
  auto-recover a crashed System to running — but it means a guest that kdump-rebooted back
  to multi-user is stopped when its core is fetched. The core was written to the overlay
  before any reboot, so it survives the force-off.
- The live harvest is `live_vm`-gated; CI verifies admission, selection, readiness, and the
  size cap with fakes. Real panic→core fidelity is a runbook/`live_vm` exercise.
- Build-id and dmesg extraction are shared with remote host_dump, so a fix to either
  benefits both providers.

## Alternatives considered

- **Live shared filesystem (virtiofs / 9p) into the guest.** Expose a host directory in the
  domain XML and point kdump at it, so the core appears host-side without an offline read.
  Rejected: it forces guest-kernel config the kdive-built kernel does not carry
  (`CONFIG_9P_FS`/`CONFIG_NET_9P_VIRTIO` or `CONFIG_VIRTIO_FS`) **into the kdump capture
  kernel and its dracut initramfs**, and reconfigures kdump's dump target away from the
  local `/var/crash` the shipped image (ADR-0188) already uses — fighting working config
  for a feature whose verification is hardware-only anyway. virtiofs additionally needs a
  per-domain `virtiofsd` and forces shared-memory backing on the whole guest.
- **Mirror remote: in-guest agent + presigned upload.** Build a local qemu-guest-agent exec
  channel and reuse `remote_libvirt/guest/`. Rejected: it adds a guest-agent dependency
  local-libvirt does not have and a network round-trip to localhost MinIO, where the host
  already owns the overlay file directly — more moving parts for no benefit on a same-host
  provider.
- **`virsh dump --memory-only` (i.e. HOST_DUMP) and call it kdump.** Rejected: that is a
  different tier (an unfiltered host-side memory image, already `CaptureMethod.HOST_DUMP`),
  not the filtered in-guest `makedumpfile` core #115 asks for; conflating them would make
  `kdump` a synonym and lose the production-fidelity distinction.
- **Read the overlay without force-stopping the domain.** Rejected: libguestfs on a disk a
  running guest is mutating yields inconsistent or corrupt reads; the System is already
  `crashed`, so a force-off is the correct, safe precondition.
