# ADR-0221: Stage the per-run DWARF vmlinux in-guest for local live drgn

- **Status:** Accepted
- **Date:** 2026-06-23
- **Deciders:** kdive maintainers

## Context

Local-libvirt live drgn introspection (`introspect.run`, ADR-0219) SSH-execs the in-guest
`kdive-drgn <helper>` program, which runs `drgn -k` against the guest's own live `/proc/kcore`.
B6 (#680) live-proved the entire drgn-live chain end-to-end — SSH transport (ADR-0218), NIC
DHCP, loopback-forward reachability, credential gate, and the in-guest helper exec all work —
and surfaced exactly one remaining gap: `drgn -k` cannot resolve typed kernel objects
(`init_uts_ns`) because **no DWARF vmlinux is staged in the guest**. `/proc/kallsyms` gives the
symbol's address, but not its type/struct layout; drgn's `-k` debuginfo finder searches
`/usr/lib/debug/lib/modules/<uname -r>/vmlinux` (and `/lib/modules/<ver>/build/vmlinux`), both
absent in-guest, so the helper exits non-zero → `DEBUG_ATTACH_FAILURE` (honest fail-fast).

Remote-libvirt satisfies this through a base-image *content obligation*: the operator-built
base image carries a matching vmlinux/debuginfo (remote's kernel is provisioned, not built
fresh). That shortcut cannot apply to local-libvirt, whose kernel is built **per Run** from
source and direct-kernel-booted — the running kernel's DWARF vmlinux is the build's `vmlinux`
artifact (the `debuginfo_ref` the offline-introspect / gdb-MI / host_dump paths already fetch
host-side), and it differs every build. So local must stage the per-Run vmlinux into the guest
overlay at install time.

The DWARF vmlinux is large (hundreds of MB to ~1 GB). The build publishes a `vmlinux`
artifact for *every* from-source build, so "stage it whenever it exists" would inject ~GB into
every from-source overlay even when live drgn is never used. A trigger is therefore needed.

`InstallRequest` today carries `kernel_ref`/`cmdline`/`method`/`initrd_ref`/`modules_ref` — no
`debuginfo_ref`. The in-guest module version (`<ver>`, e.g. `7.0.0`) used for every staged
path is derived solely from the injected modules tarball's `lib/modules/<ver>/` top directory;
there is no separately recorded kernel-release string.

## Decision

Thread the Run's `debuginfo_ref` into the install path and stage the DWARF vmlinux into the
System overlay at `/usr/lib/debug/lib/modules/<ver>/vmlinux`, **riding the existing kdump
modules-injection rw-libguestfs session** (which already derives `<ver>` from the modules
tarball, force-offs the domain, and mounts the overlay read-write).

- `InstallRequest` gains `debuginfo_ref: str | None = None` (additive, optional;
  remote-libvirt / fault-inject ignore it — no behavior change there).
- `runs_install` resolves it via a new `installed_debuginfo_ref(conn, run_id)` reader
  (mirroring `installed_modules_ref`) and passes it.
- The local installer's injection trigger becomes
  `request.modules_ref is not None and (request.method is CaptureMethod.KDUMP or
  request.debuginfo_ref is not None)`: it fires for kdump exactly as before, and additionally
  whenever a `debuginfo_ref` is present (the drgn-capable from-source signal). The modules
  tarball is required either way — it is the only `<ver>` source and the rw session host.
- `GuestKernelWriter.inject` gains an optional `vmlinux: Path | None`; when present it uploads
  the vmlinux to `/usr/lib/debug/lib/modules/<ver>/vmlinux` (idempotent `mkdir_p` + truncating
  `upload`) and verifies a non-empty size, in the same rw session that stages modules + kernel.
- The local installer fetches the vmlinux to the per-Run staging dir over the existing
  artifact-fetch seam and passes it to the writer.

`introspect.run` maturity stays `partial`; B6 (#680) promotes it after a live KVM
`introspect.run` round-trip on a System installed by this code, closing Epic B #682.

## Consequences

- A local drgn-live System (from-source build → `debuginfo_ref` present, modules published)
  gets its matching DWARF vmlinux staged at the drgn-discoverable path, so the in-guest
  `kdive-drgn` helper resolves typed symbols against `/proc/kcore`.
- The vmlinux is staged only for builds that produce debuginfo **and** publish modules; a
  non-debug build pays nothing. The ~GB cost falls only on debug/kdump installs.
- A HOST_DUMP System that has both `modules_ref` and `debuginfo_ref` now also triggers the
  rw-injection it previously skipped (force-off + modules + kernel + vmlinux). This is the
  cost of drgn-live readiness; modules/kernel staging is harmless for such a System.
- Provider-neutral `InstallRequest` carries a field only local-libvirt reads today. It is
  optional and defaulted, so remote-libvirt / fault-inject are unaffected.
- The live `upload`/`statns` paths stay `live_vm`-gated; the trigger, fetch threading,
  destination-path composition, and size sentinel are unit-tested with fakes.

## Considered & rejected

- **Thread a `stage_live_debuginfo` debug-intent flag from the provisioning profile.** The
  most general trigger (works for a non-kdump, non-modules drgn-live System), but more
  plumbing (profile → handler → request) for a combination the standard local debug System
  (kdump-armed, modules-publishing) does not need. Deferred until a non-modules drgn-live
  System is real; the chosen trigger covers every System the milestone exercises.
- **Always stage the vmlinux whenever `debuginfo_ref` exists.** Simplest, but injects ~GB into
  every from-source overlay regardless of debug intent.
- **A decoupled vmlinux-only injection session** (independent of modules). Needs a `<ver>`
  source other than the modules tarball, which does not exist (no recorded kernel release),
  and a second rw-mount/force-off; rejected for the extra IO and the version-discovery work.
- **Inject the vmlinux into the debug rootfs at `build-fs` time** (like remote's base-image
  obligation). Impossible — the per-Run kernel is unknown when the generic debug rootfs is
  built; the vmlinux must match the running kernel.
- **Tunnel `/proc/kcore` + vmlinux to the worker and run drgn host-side** (the offline model).
  Rejected by ADR-0219 already: gigabytes of fragile live-memory IO; drgn `-k` is built to run
  in-process in the guest.
