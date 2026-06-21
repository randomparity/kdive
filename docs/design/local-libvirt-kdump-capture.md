# Local-libvirt Tier 3 capture: in-guest kdump vmcore harvest

- **Issue:** #115 (feat: Tier 3 capture — full in-guest kdump / vmcore, `provider:local-libvirt`)
- **ADR:** [0203](../adr/0203-local-libvirt-kdump-overlay-harvest.md)
- **Status:** design

## Problem

`CaptureMethod.KDUMP` is the production-fidelity capture tier: the guest itself runs
`makedumpfile` under a `kexec`-loaded crash kernel and writes a filtered
`/var/crash/<timestamp>/vmcore` to its own disk. The MCP surface (`vmcore.fetch`), the
crashkernel cmdline wiring (ADR-0030/0051, #116), the multi-distro kdump-capable guest
image catalog (ADR-0188, #598), and a full remote-libvirt KDUMP implementation
(ADR-0084) already exist. Local-libvirt is the gap:

- `LocalLibvirtRetrieve` advertises only `{CONSOLE, HOST_DUMP, GDBSTUB}` in
  `supported_capture_methods`, so `vmcore.fetch(method=kdump)` on a local System is
  rejected at admission with `method not supported by provider`
  (`mcp/tools/lifecycle/vmcore.py`).
- The `_real_wait_for_vmcore` and `_real_extract_redacted` seams in
  `providers/local_libvirt/retrieve.py` are placeholders that raise `MISSING_DEPENDENCY`.

The boot side is already wired: `LocalLibvirtProfilePolicy.capture_method` returns
`KDUMP` when the profile sets `crashkernel`, `system_required_cmdline` adds
`crashkernel=256M` for a KDUMP install, and the install preflight refuses a KDUMP boot
whose initramfs carries no capture hook (`_kdump_capture_present`). A profile can
therefore *boot* a local kdump-capable System but cannot *capture* its core.

## Current state (what already shipped — not in scope)

| Element | Where | State |
|---|---|---|
| `KDUMP` enum + `vmcore.fetch(method=kdump)` admission | `domain/capture.py`, `mcp/tools/lifecycle/vmcore.py` | done |
| Crashkernel reservation in the boot cmdline (method-conditional) | `services/runs/steps.py`, ADR-0030/0051 | done |
| Kdump-capable guest image (kexec-tools/makedumpfile/kdump service) | `deploy/ansible/.../guest_base_image`, ADR-0188 | done |
| Remote-libvirt KDUMP capture (guest agent + presigned upload) | `providers/remote_libvirt/retrieve/kdump_capture.py`, ADR-0084 | done |
| Build-id from VMCOREINFO + dmesg via drgn | `providers/remote_libvirt/retrieve/host_dump_capture.py` | done (reusable) |

## Goal / non-goals

**Goal.** Make `CaptureMethod.KDUMP` a working capture method on local-libvirt: harvest the
guest-written vmcore host-side, store the raw + redacted artifacts, and return the build-id
— so `vmcore.fetch(method=kdump)` followed by `postmortem.crash`/`.triage` works against a
deliberately crashed local System.

**Non-goals.** No change to the MCP tool surface, the job/admission path, the boot/install
plane, the guest image catalog, the database schema, or any other provider. No change to
how HOST_DUMP, CONSOLE, or GDBSTUB behave on local-libvirt.

## Approach: host-side overlay harvest (ADR-0203)

Local QEMU domains run on the same host as the kdive worker. The guest writes its filtered
core to `/var/crash/<timestamp>/vmcore` **on its own root disk** — the per-System qcow2
overlay at `local_libvirt/lifecycle/storage.py::overlay_path(system_id)`. The harvest reads
that file directly out of the overlay host-side, with no guest agent and no live shared
filesystem:

1. **Quiesce.** Open the local libvirt connection, look up the domain, and force it off
   (`destroy`, idempotent if already shut off). The System is `crashed` when `vmcore.fetch`
   admits (the tool gates on `SystemState.CRASHED`), so force-off is consistent with its
   state and is what makes the overlay safe to read offline. libguestfs explicitly warns
   against reading a disk a running VM is writing.
2. **Locate.** Mount the overlay read-only via libguestfs and list candidate cores —
   `/var/crash/*/vmcore` (depth-bounded), newest by mtime first — mirroring the in-guest
   helper's `find $CRASH_DIR -maxdepth 2 -name vmcore | sort -rn | head -n1` convention.
3. **Read.** Read the newest core's bytes (subject to the shared 5 GiB single-object
   ceiling, `MAX_CORE_BYTES`). No matching core within the readiness window → `None`, which
   `LocalLibvirtRetrieve.capture` already turns into a `READINESS_FAILURE`.
4. **Extract.** Build-id and redacted dmesg come from the harvested core via the existing
   drgn helpers shared with remote host_dump (`read_core_build_id_from_file`,
   `read_core_dmesg_from_file`); dmesg extraction degrades to the `DMESG_UNAVAILABLE`
   sentinel when the guest kernel's printk buffer can't be read without debuginfo, exactly
   as remote host_dump does.
5. **Advertise.** Add `CaptureMethod.KDUMP` to local-libvirt's `supported_capture_methods`
   so admission accepts it.

### Testability split

The seam boundary keeps logic CI-testable and isolates the hardware edge:

- **Pure, unit-tested (fakes):** newest-core selection (newest-wins, ties, empty, malformed
  listing), the readiness/timeout poll loop, the `> MAX_CORE_BYTES` rejection, the
  `absent → None → READINESS_FAILURE` contract, and `supported_capture_methods` now
  including KDUMP (admission accepts `kdump` for local, rejects unknown). These drive a
  `GuestCoreReader` protocol (`list_vmcores`, `read_vmcore`) with an in-memory fake.
- **`# pragma: no cover - live_vm` edge:** the real libguestfs mount/read, the
  `domain.destroy()` force-off, and the drgn build-id/dmesg calls. Selected by `from_env`,
  exercised only under the `live_vm` marker.

### Failure contract

| Condition | Category |
|---|---|
| libguestfs not installed on the worker host | `MISSING_DEPENDENCY` |
| drgn not installed (build-id/dmesg) | `MISSING_DEPENDENCY` (reused remote helper) |
| no complete core in the readiness window | `READINESS_FAILURE` (existing `capture` contract) |
| core exceeds the 5 GiB single-object ceiling | `CONFIGURATION_ERROR` |
| libvirt/libguestfs IO failure mid-harvest | `INFRASTRUCTURE_FAILURE` |
| core present but VMCOREINFO has no BUILD-ID | `CONFIGURATION_ERROR` (reused remote helper) |

## Acceptance

CI-verifiable (this PR, unit tests):

- local-libvirt `supported_capture_methods` includes `KDUMP`; `vmcore.fetch(method=kdump)`
  on a local crashed System is admitted (no longer `method not supported by provider`).
- newest-core selection, readiness/timeout, size-cap, and `None → READINESS_FAILURE` paths
  are covered with fakes.
- the full existing `LocalLibvirtRetrieve.capture` orchestration still passes for KDUMP via
  injected seams.

Live-hardware only (`live_vm` marker + runbook, not CI): a guest that boots with
`crashkernel=` and a deliberately panicked System yields a drgn-loadable
`/var/crash/vmcore` harvested by `vmcore.fetch(method=kdump)`, consumable by
`postmortem.crash`. A `live_vm` test asserts the real harvest; the runbook documents the
host prerequisite (`libguestfs`) and the panic→capture→postmortem walk.

## Documentation

- The `vmcore.fetch` maturity meta `providers=` line gains `local-libvirt: …/KDUMP`.
- The local-libvirt runbook gains a Tier 3 (kdump) capture section: host prerequisite,
  profile `crashkernel`, and the panic→fetch→postmortem sequence.
