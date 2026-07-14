# Multiarch gdb selection for cross-arch debug sessions (#1149)

Date: 2026-07-14
Status: approved (design)
Issue: #1149 — sub-issue 10 of epic #1139 (full ppc64le support)
Depends on: #1144 (live TCG boot proof)
ADR: [0347](../adr/0347-cross-arch-gdb-binary-selection.md)

## Goal

A gdb-MI debug session attaches to a guest whose architecture differs from the host's — the
epic's target is a **ppc64le guest under TCG on an x86_64 host** — by spawning a
multiarch-capable gdb and targeting the guest arch, with a doctor check that flags the missing
multiarch prerequisite before a live attach fails opaquely.

## Background

The gdb-MI tier (`providers/shared/debug_common/gdbmi/`, ADR-0034/0248) drives a persistent
`gdb --interpreter=mi3` over QEMU's gdbstub. The stub speaks the guest arch; the MI layer is
already arch-neutral (register names come from `-data-list-register-names`, not a hardcoded
x86 file). The single arch-blind spot is host-side: `GdbMiEngine.attach` spawns a fixed `gdb`
binary and never sets a target architecture.

- On split-gdb distros (Debian/Ubuntu) the cross-capable build is `gdb-multiarch`; plain `gdb`
  targets only the host arch.
- On build-multiarch distros (Fedora — the validation host) plain `gdb` already targets every
  arch and there is no `gdb-multiarch` package.

`attach` already resolves and loads the guest `vmlinux` (`-file-exec-and-symbols`) **before**
connecting the stub. That ELF's `e_machine`/`EI_DATA` header is the guest arch — the ground
truth for the symbols gdb loads — available exactly where the gdb subprocess is spawned.

## Design

Per [ADR-0347](../adr/0347-cross-arch-gdb-binary-selection.md):

### 1. Arch helpers (new, pure, unit-tested)

A new module `providers/shared/debug_common/gdbmi/policy/arch.py`:

- `arch_from_elf(path) -> str | None` — read the 20-byte ELF prefix; map `e_machine` (+
  `EI_DATA` endianness for powerpc) to a kdive arch string. `EM_X86_64` → `x86_64`; `EM_PPC64`
  little-endian → `ppc64le`. Non-ELF, truncated, or unrecognized → `None`.
- `select_gdb_binary(host_arch, guest_arch, which) -> str | None` — native (`guest == host` or
  `guest is None`) → `which("gdb")`; cross → `which("gdb-multiarch") or which("gdb")`. Returns
  the resolved path or `None`. `which` is injected (`shutil.which` in production).
- `gdb_target_arch_name(arch) -> str | None` — kdive arch → gdb `set architecture` name
  (`x86_64` → `i386:x86-64`, `ppc64le` → `powerpc:common64`); unknown → `None`.

Arch strings are validated against `arch_traits.SUPPORTED_ARCHES`; the gdb-name and ELF-machine
tables live in this module (debugger specifics, kept out of the domain `arch_traits`).

### 2. Engine attach wiring

`GdbMiEngine.attach` (`.../gdbmi/core/engine.py`):

- Inject `host_arch_finder: Callable[[], str] = platform.machine` on `__init__` (testable).
- Reorder: resolve + validate the `vmlinux` first, then derive `guest_arch = arch_from_elf(...)`
  and `host_arch = self._host_arch()`, then `gdb_path = select_gdb_binary(host_arch, guest_arch,
  self._gdb_path_finder)`.
- `gdb_path is None` → `MISSING_DEPENDENCY`. Cross-arch names `gdb-multiarch` + hint; native
  keeps the existing "missing required gdb" message and `missing_tools=["gdb"]`.
- Spawn the selected binary. After `-file-exec-and-symbols` and before `-target-select remote`,
  on the cross path only, if `gdb_target_arch_name(guest_arch)` is known, issue
  `-gdb-set architecture <name>`. Native path unchanged (no explicit set).

This is `# pragma: no cover - live_vm`; the helpers carry the unit coverage.

### 3. Doctor check

- `diagnostics/checks.py`: add `MULTIARCH_GDB_ID = "multiarch_gdb"`.
- `diagnostics/provider_checks.py`: add `MultiarchGdbOutcome` (`supported` / `missing` /
  `undeterminable`), `MultiarchGdbProbe = Callable[[], Awaitable[MultiarchGdbOutcome]]`, and
  `MultiarchGdbCheck(Check)` (vantage `WORKER`). Outcome → `CheckResult`: `supported` → `pass`;
  `missing` → `fail` + fix (`install gdb-multiarch (Debian/Ubuntu) or a multiarch gdb build`) +
  `failure_category=MISSING_DEPENDENCY`; `undeterminable` → `error`.
- `diagnostics/result_codec.py`: add `MULTIARCH_GDB_ID` to `_ALLOWED_IDS` (worker→server inline
  transport).
- New `providers/local_libvirt/diagnostics/contribution.py`: the real probe and
  `diagnostic_contribution() -> DiagnosticProviderContribution` (provider `local-libvirt`,
  `enabled` always true, this one worker check). The probe gates on kdive's **static**
  cross-arch capability, not per-host libvirt schedulability, so a worker-vantage check needs
  no DB handle and no libvirt call: the foreign arch set is `arch_traits.SUPPORTED_ARCHES −
  {platform.machine()}`. For each foreign arch it finds the candidate gdb
  (`select_gdb_binary`) and runs it in batch (`--batch -nx -ex "set architecture <gdb-name>"`,
  exit 0 = accepted). Outcome: every foreign arch targetable (or no foreign arch, i.e. the host
  arch is the only supported arch) → `supported`; some foreign arch has no gdb that can target
  it → `missing`; the candidate could not be run to a verdict (spawn error) → `undeterminable`.
- `providers/assembly/diagnostics.py`: register the local-libvirt contribution alongside remote.

### 4. Agent-facing contract

The debug-session tool wrapper docstring (the schema the agent reads) notes that a cross-arch
attach requires a multiarch-capable gdb on the worker host and points at the doctor check when
the attach fails with `MISSING_DEPENDENCY`.

## Acceptance criteria

- **AC1** Unit tests for `select_gdb_binary` across `(host, guest)` pairs: native → `gdb`;
  cross with `gdb-multiarch` present → `gdb-multiarch`; cross with only `gdb` → `gdb`; cross
  with neither → `None`; guest `None` → native.
- **AC2** Unit tests for `arch_from_elf` on `EM_X86_64`, `EM_PPC64`-LE fixtures, and
  non-ELF/truncated input (→ `None`); and `gdb_target_arch_name` for known/unknown arches.
- **AC3** `MultiarchGdbCheck` maps each `MultiarchGdbOutcome` to the right three-state
  `CheckResult` (a `fail` carries the install hint and `MISSING_DEPENDENCY`); `multiarch_gdb`
  round-trips through `result_codec`.
- **AC4** The local-libvirt diagnostic contribution is assembled and its probe selects the
  right candidate and classifies missing/present.
- **AC5** Live smoke (documented, on the x86_64 host): gdb attaches to a ppc64le guest's
  gdbstub from the x86_64 host and reads registers. The pass signal must **discriminate the
  arch**, not merely return non-empty: `-data-list-register-names` yields the ppc64le GPR/SPR
  set (`r0`…`r31`, `pc`/`nip`) and none of the x86 names (`rax`), so the proof fails if gdb
  targeted the wrong architecture rather than only if the stub was silent. Recorded as a proof
  note; `UNVERIFIED` only if the host cannot run the proof at all.

## Out of scope

- remote-libvirt cross-arch doctor check (separate provider epic; the shared engine change
  benefits it for free).
- drgn cross-arch (issue 11) and the `live_vm` arch matrix wiring (issue 15).
- Big-endian ppc64, and any arch beyond `arch_traits.SUPPORTED_ARCHES`.
