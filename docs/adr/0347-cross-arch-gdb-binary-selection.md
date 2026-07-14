# ADR 0347 — Cross-arch gdb binary selection: derive the guest arch from the staged vmlinux

- **Status:** Accepted
- **Date:** 2026-07-14
- **Issue:** #1149
- **Epic:** #1139 (full ppc64le support)
- **Builds on:** ADR-0034 (gdb-MI tier over the gdbstub), ADR-0248 (persistent gdb-MI engine),
  ADR-0338/0339 (guest-arch discovery + persisted System accel), ADR-0091 (three-state
  diagnostic checks), ADR-0272 (baseline direct-kernel boot)

## Context

QEMU's gdbstub speaks the *guest* architecture. The shared gdb-MI layer already resolves
register names dynamically (`-data-list-register-names`, ADR-0034) rather than hardcoding an
x86 register file, so the MI protocol layer is arch-neutral. The remaining gap is host-side:
the engine's `attach` (`providers/shared/debug_common/gdbmi/core/engine.py`) spawns a fixed
`gdb` binary. When the guest arch ≠ the host arch (a ppc64le guest under TCG on an x86_64
host, the epic's target), a host's *native-only* gdb cannot target the guest. Distros that
split gdb ship the cross-capable build as `gdb-multiarch` (Debian/Ubuntu); distros that build
gdb multiarch-by-default ship it as plain `gdb` (Fedora). Nothing in the debug plane selects
the right binary, and no diagnostic surfaces the missing prerequisite before a live attach
fails with an opaque gdb error.

Two facts shape the decision:

1. **The engine already stages and loads the guest `vmlinux` before connecting the stub.**
   `attach` runs `-file-exec-and-symbols <vmlinux>` *before* `-target-select remote`. That
   ELF's `e_machine`/`EI_DATA` header *is* the guest arch — it is the ground truth for the
   symbols gdb will use, and it is available at the exact point the gdb subprocess must be
   spawned. So the guest arch need not be threaded through the `AttachSeam` from the System
   row; it can be read from the file the engine already resolves.
2. **The gdbstub advertises a target description**, so a multiarch-capable gdb usually
   auto-detects the architecture on connect. But that inference can be absent or wrong; the
   loaded `vmlinux` ELF is the authoritative signal, and an explicit `set architecture` on the
   cross-arch path removes the ambiguity the issue calls out.

## Decision

**Derive the guest arch from the staged `vmlinux` ELF header at attach time; select the gdb
binary by comparing it to the host arch; add a local-libvirt worker-vantage diagnostic that
reports a missing multiarch gdb with an actionable install hint.**

Concretely:

- **Guest arch from the ELF, not the System row.** A pure helper `arch_from_elf(path)` reads
  the ELF `e_machine` (+ `EI_DATA` endianness) and maps it to a kdive arch string
  (`EM_X86_64` → `x86_64`; `EM_PPC64` little-endian → `ppc64le`); an unreadable or unrecognized
  header returns `None`, which the engine treats as "assume native" (plain `gdb`) — a safe
  fallback that never blocks a same-arch attach. This keeps the change to the engine +
  helpers; the `AttachSeam` protocol and the three providers are untouched.
- **Name-based binary selection with fallback.** A pure helper
  `select_gdb_binary(host_arch, guest_arch, which)`:
  - native (`guest == host`, or guest unknown) → `which("gdb")`;
  - cross (`guest != host`) → `which("gdb-multiarch") or which("gdb")` — prefer the split
    package where present, else fall back to plain `gdb`, which *is* multiarch on
    build-multiarch distros.
  It returns the resolved path or `None`. `None` on the cross path makes `attach` raise
  `MISSING_DEPENDENCY` naming `gdb-multiarch` with the install hint; `None` on the native path
  raises the existing "missing required gdb" error. The `which` seam is injected, so the
  selection is unit-tested across every `(host, guest)` pair without a real gdb.
- **Explicit target arch on the cross path.** After loading symbols and before connecting the
  stub, a cross-arch attach issues `-gdb-set architecture <gdb-name>` where a known mapping
  exists (`x86_64` → `i386:x86-64`, `ppc64le` → `powerpc:common64`); an unknown arch sets
  nothing and lets gdb infer. The native path sets nothing (gdb's default is correct).
- **Doctor check.** A `MultiarchGdbCheck` (worker vantage, id `multiarch_gdb`) maps an injected
  `MultiarchGdbProbe` outcome to a three-state `CheckResult`: every supported foreign arch is
  targetable → `pass`; a supported foreign arch has no gdb that can target it → `fail` with the
  `apt install gdb-multiarch` / distro hint; the probe could not run → `error`. The real probe
  lives in `diagnostics/multiarch_gdb.py` — it attributes to `local-libvirt` but depends on no
  local-libvirt internals, so it stays in the neutral diagnostics package rather than behind the
  provider-assembly seam (the boundary guard reserves `providers/local_libvirt/*` imports for
  `composition.py`) — and gates on kdive's **static** cross-arch
  capability — the foreign set is `arch_traits.SUPPORTED_ARCHES − {host arch}`, so a
  worker-vantage check needs no DB handle and no libvirt call — running a candidate gdb in batch
  mode to confirm it accepts each foreign `set architecture`; a host whose only supported arch is
  its own reports `pass`. The check id is added to the worker→server `_ALLOWED_IDS` allowlist so
  its verdict survives inline transport.

## Consequences

- Cross-arch gdb attach picks a multiarch-capable binary automatically; a same-arch attach is
  byte-identical to today (native `gdb`, no explicit `set architecture`).
- The guest arch has one source on the debug path — the `vmlinux` the engine already loads —
  so there is no second, drift-prone arch field threaded through the attach seam, and no DB
  read added to the attach hot path. The persisted `System.accel` (ADR-0339) is unaffected;
  arch and accel stay separate facts.
- A missing multiarch gdb is a doctor `fail` with a fix, not an opaque runtime attach error.
- The change benefits remote-libvirt's shared engine for free, but the doctor check is scoped
  to local-libvirt (remote arch work is a separate epic).
- No migration, no schema change, no new dependency (ELF parsing is a fixed-offset header
  read; no `pyelftools`).

## Rejected alternatives

- **Thread the persisted `System.provisioning_profile.arch` through the `AttachSeam`.**
  Rejected: it changes the `AttachSeam` protocol and all three providers
  (local/remote/fault-inject) plus the debug runtime, and adds a DB read to the attach path,
  to obtain a fact the engine can read from the `vmlinux` it already stages. The ELF is the
  authoritative arch for the symbols gdb loads; the row would only duplicate it.
- **Always require `gdb-multiarch` (never fall back to plain `gdb`).** Rejected: it would
  break cross-arch debug on Fedora, where plain `gdb` is the multiarch build and no
  `gdb-multiarch` package exists. Name-based preference with fallback covers both distro
  families.
- **Probe every candidate gdb for multiarch capability at *attach* time.** Rejected as latency
  on the hot path: the attach picks by name (cheap, deterministic) and the *doctor* runs the
  heavier capability probe out of band, which is exactly what a preflight check is for.
- **Never set `architecture` explicitly; trust the stub's target description.** Rejected as
  fragile: the issue explicitly calls for handling the case where gdb cannot infer the arch
  from the stub. The loaded `vmlinux` is authoritative, and an explicit set on the cross path
  is a harmless no-op when inference already agrees.
- **Add a `WARN`/advisory severity so a native-only host is not a `fail`.** Rejected: the
  three-state model (ADR-0091) is deliberate, and diagnostics are a reporting surface, not an
  admission gate — a `fail` with an actionable fix is the right signal when the host cannot
  debug a supported foreign arch. The probe gates `fail` on a *supported* foreign arch
  (`arch_traits.SUPPORTED_ARCHES − {host arch}`), so a host whose only supported arch is its own
  reports `pass` — no coupling to per-host libvirt schedulability.
