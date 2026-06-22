# ADR 0211 — Local-libvirt host-dump capture via the libvirt domain core dump

- **Status:** Accepted
- **Date:** 2026-06-22
- **Deciders:** kdive maintainers
- **Issue:** M2.8 B4
- **Builds on:** [ADR-0050](0050-vmcore-method-aware-storage.md) (method-aware vmcore storage,
  first-method-wins per System), [ADR-0203](0203-local-libvirt-kdump-overlay-harvest.md) (the
  existing local kdump capture path this complements), [ADR-0208](0208-provider-capability-descriptor.md)
  (the descriptor this flips on), [ADR-0209](0209-capability-aware-mcp-admission.md) (the
  profile-resolved `vmcore.fetch` default this makes honest for local).
- **Spec:** authored per-issue during `work-issue` (B4), alongside this ADR.

## Context

Local-libvirt advertises `CaptureMethod.HOST_DUMP` in `supported_capture_methods`, `vmcore.fetch`
defaults to it, and `LocalLibvirtRetrieve.capture` branches to `_capture_host_dump` for it — but
the underlying `_real_host_dump_capture` seam **raises `MISSING_DEPENDENCY` unconditionally**
("real host-dump capture runs only under the live_vm gate"). So the default `vmcore.fetch` call
on a local System fails. The only capture method local genuinely implements is `KDUMP` (the
ADR-0203 libguestfs overlay harvest), which requires the guest to be configured with a crash
kernel (and, per #666/ADR-0207, a kernel staged into `/boot`).

Host-dump is the deterministic, guest-cooperation-free capture path: the hypervisor dumps the
domain's memory directly, with no in-guest kdump arming, no crashkernel reservation, and no
`/var/crash` harvest. Remote-libvirt already supports both KDUMP and HOST_DUMP (M2.5); local
should have the same dual-mode shape. The mechanism is native to libvirt — `virDomainCoreDumpWithFlags`
(`virsh dump`) writes the running/crashed domain's memory image to a host path the worker reads —
and the development host has libvirt `qemu:///session` available to prove it.

## Decision

Implement `_real_host_dump_capture` against the **libvirt domain core dump**, giving local-libvirt
a real HOST_DUMP path with the same method-aware storage contract as KDUMP, and making
ADR-0209's profile-resolved `vmcore.fetch` default honest for a `preserve_on_crash` local System.

### 1. Capture via `virDomainCoreDumpWithFlags` to a worker-readable path

The seam dumps the System's domain memory to a private worker temp path via the libvirt domain
core-dump API (the memory-only, crash-format flags appropriate for a vmcore the downstream
`crash`/drgn postmortem reads), then streams the file into the object store exactly as the KDUMP
path does (raw + redacted, build-id read from the core, ADR-0050 method-encoded key
`vmcore-host_dump`). The whole-core bytes are never held in one in-memory buffer — the same
file-streaming discipline as the KDUMP harvest (#657).

### 2. Storage and method semantics reuse the existing contract

HOST_DUMP stores under the method-encoded key (ADR-0050 first-method-wins per System), so a
System captured via host-dump and one captured via kdump never collide, and `vmcore.fetch`'s
existing same-method-dedup / different-method-conflict checks apply unchanged. No new storage
shape.

### 3. The descriptor flips and the `vmcore.fetch` tool maturity promotes with the wiring

A1 narrowed local's `supported_capture_methods` to `{KDUMP}` (host-dump was stubbed); this ADR
adds `HOST_DUMP` back, now **truthfully**, since the seam is real. With both of local's
core-producing methods working — kdump (proven live in B5) and host-dump (this issue) — the
`vmcore.fetch` **tool's** ADR-0175 maturity promotes to `implemented` (host-dump was its blocking
partial reason); this is why B4 depends on B5's kdump live proof. Per ADR-0209 the omitted-method
default stays profile-resolved through `capture_method(profile)` (a `preserve_on_crash` local
System now resolves a working `HOST_DUMP`; a crashkernel System resolves `KDUMP`) — no flat
provider default. Until B4 lands, ADR-0209 fail-fast already rejects a HOST_DUMP request on local
with a clear `configuration_error` rather than the deferred `MISSING_DEPENDENCY`.

### 4. Seam split + live proof

The capture orchestration (branch, store, redact, build-id, method-key) is already real and
unit-tested with a fake `host_dump_capture`; only the libvirt core-dump call joins the existing
`# pragma: no cover - live_vm` real seam. B4 closes only after a live drive on the development
KVM host proves a real `virsh dump` → stored vmcore → `crash`/drgn postmortem round-trip.

## Consequences

- `vmcore.fetch` HOST_DUMP works on local-libvirt without any guest kdump configuration — the
  deterministic capture path that complements KDUMP, matching remote's dual-mode shape.
- No port, schema, or migration change — the existing `Retriever.capture` contract and ADR-0050
  storage are reused; the change is the one seam + the descriptor/maturity flip.
- Local now has two real capture paths: KDUMP (guest-cooperative, needs crashkernel + #666 kernel
  staging) and HOST_DUMP (hypervisor-side, no guest cooperation). An agent picks per need, and the
  surface reports both honestly.

## Considered & rejected

- **Leave HOST_DUMP unimplemented and drop it from local's `supported_capture_methods`.** Rejected:
  host-dump is the *easier*, more deterministic path on local (no guest arming) and the one remote
  already offers; dropping it would leave local with only the configuration-dependent KDUMP path
  and diverge the providers. Implement it, don't retract the advertisement.
- **Implement host-dump by reading guest memory over the gdbstub/QMP instead of `virsh dump`.**
  Rejected: `virDomainCoreDumpWithFlags` is the purpose-built libvirt primitive that produces a
  crash-format core the existing `crash`/drgn postmortem consumes; reconstructing one from a
  gdbstub memory walk would reinvent it less reliably.
- **Hold the whole core in memory like the legacy `_capture_host_dump` bytes path.** Rejected: a
  vmcore can be gigabytes; the KDUMP path already streams from a worker temp file (#657) and
  host-dump reuses that discipline rather than the in-memory buffer.
