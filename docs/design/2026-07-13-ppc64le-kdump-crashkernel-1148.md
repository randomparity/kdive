# kdump on ppc64le — per-arch crashkernel defaults and capture proof (#1148)

Date: 2026-07-13
Status: approved (design)
Issue: #1148 · Epic: #1139 (full ppc64le support) · ADR: `docs/adr/0346-ppc64le-kdump-crashkernel-defaults.md`
Depends on: #1146 (uploaded-bundle direct-kernel boot, ADR-0344), #1147 (unified customization boot, ADR-0345)

## Problem

Three gaps block kdump on a ppc64le guest, only one of which the issue title names outright.

1. **The crashkernel default is x86-sized and single-valued.** `system_required_cmdline`
   (`services/runs/steps.py:372`) falls back to a module-level `DEFAULT_CRASHKERNEL = "256M"`
   for every arch when no per-install reservation is given (ADR-0300). 256M is the x86_64
   default; ppc64le distros reserve more (RHEL 10's `kdump-utils` floor is **384M** for a
   2–4 GB guest, **512M** for 4–16 GB — roughly double x86). A ppc64le guest that boots with
   `crashkernel=256M` risks a kdump kernel that OOMs before it can run makedumpfile, so the
   capture silently produces no vmcore.

2. **The kdump capture path is blocked cross-arch — discovered live in #1146, deferred here.**
   A KDUMP install fires module injection (`install.py:339`:
   `needs_modules = request.method is KDUMP or debuginfo_ref is not None`), whose
   `_RealGuestKernelWriter._extract_and_index` runs `guest.command(["depmod", "-a", version])`
   inside libguestfs — i.e. the **guest's** ppc64le `depmod` ELF executed in libguestfs's
   **x86_64** appliance. #1146's proof record (Result 2) reproduced the failure:
   `depmod: Exec format error`. Module injection is load-bearing for kdump (the guest's
   `kdumpctl` needs `/lib/modules/<ver>` + `/boot/vmlinuz-<ver>` to build and kexec-load a
   capture kernel), so **a ppc64le kdump install cannot complete** until this is fixed. #1146's
   ADR-0344 explicitly scoped the accommodation to "issue 9 (kdump)".

3. **The pseries VMCOREINFO/fw_cfg story is unverified.** PR #1070 deliberately left it open;
   the epic's "Known unverified" list flags it (issue 9). The x86 domain emits
   `<features><acpi/><vmcoreinfo state="on"/></features>` (`xml.py:223`), gated x86-only by
   `arch_traits.emit_acpi_features`. Whether a ppc64le kdump capture needs any `<features>`
   emission is an empirical question this issue must answer, not assume.

## Inputs (already landed)

- **ADR-0300 / #989**: the per-install `crashkernel` reservation seam — `runs.install` takes an
  optional `crashkernel`, threaded through `InstallPayload` → `cmdline_for` →
  `system_required_cmdline(..., crashkernel=…)`, where a supplied value replaces the default in
  the `crashkernel=<size>` token. This is the "tunable seam" the issue references: **an explicit
  reservation already wins over the default**; only the *default itself* changes here.
- **ADR-0338/0339/0340/0341 / #1140–1143**: admission persists `accel=tcg` for a ppc64le guest
  on the x86_64 host; the provisioner renders a pseries/qemu domain with `console=hvc0`, no
  `<cpu>` under TCG, and no x86 `<features>` block; the boot handler applies the TCG-scaled
  readiness deadline.
- **ADR-0343 / #1145**: the arch-aware upload contract — a ppc64le combined tar (ELF64-LE
  `EM_PPC64` at `boot/vmlinuz` + `lib/modules/<ver>/`) validates at `runs.complete_build`.
- **ADR-0344 / #1146**: uploaded ppc64le bundles direct-kernel-boot on pseries under TCG; the
  install/boot host-side staging is arch-opaque; the cross-arch `depmod` constraint is recorded
  and handed to this issue.
- `domain/platform/arch_traits.py`: the per-arch trait table (`machine`, `console_device`,
  `pin_nic_slot`, `kvm_cpu_mode`, `emit_acpi_features`), the intended home for the crashkernel
  default per the epic design ("per-arch `crashkernel` defaults move into the arch-traits table").

## Design

### 1. Per-arch crashkernel default in `arch_traits` (the headline AC)

`ArchTraits` gains a `default_crashkernel: str` field; `system_required_cmdline` resolves the
fallback from the arch, not a module constant:

- `x86_64.default_crashkernel = "256M"` — **unchanged** value (AC: "x86_64 value unchanged").
- `ppc64le.default_crashkernel = "512M"` — the single-value default. It clears RHEL's 384M floor
  for the smallest supported guest range (2–4 GB) with margin and matches the 4–16 GB
  reservation, so a small TCG guest reserves enough for the capture kernel without a range token
  (kdive's default is single-valued to match the existing x86 shape; an operator wanting a
  range passes it explicitly through the ADR-0300 seam).
- `system_required_cmdline` changes its one line from `crashkernel or DEFAULT_CRASHKERNEL` to
  `crashkernel or arch_traits(arch).default_crashkernel`. The module-level `DEFAULT_CRASHKERNEL`
  is **removed** (replace-don't-deprecate); its two remaining readers are updated (§1a).

The seam order is preserved and the AC "profile/policy overrides still win" holds by
construction: an explicit `crashkernel` argument (the ADR-0300 per-install reservation) is still
`… or …`-preferred over the arch default; only the `None` fallback is now arch-keyed.

#### 1a. Agent-facing and read-back text (the tool contract)

The default is now arch-dependent, so the `runs.install` wrapper `Field` text and docstring
(the FastMCP-serialized agent contract, `mcp/tools/lifecycle/runs/registrar.py`) must stop
asserting a single "256M" default. They state the per-arch defaults **derived from the trait
table** (not a re-typed literal, so they cannot drift): "reverts … to the platform per-arch
default (256M on x86_64, 512M on ppc64le)". A single-source helper renders that summary from
`_TRAITS` so adding an arch updates the agent text automatically. `runs.get`'s
`data.required_cmdline` already recomposes `system_required_cmdline` with no explicit
reservation, so it will surface the correct per-arch default with no change — the effective
reserved size stays agent-visible even when `data.installed_crashkernel` is `None` (client
passed no override). The `step_progress` docstring note "the default 256M was in force" is
generalized to "the arch default was in force".

### 2. Cross-arch module indexing: host-side `depmod` (unblocks kdump capture)

`depmod` is arch-neutral **as a tool** — it parses each module's ELF header and `.modinfo`,
resolves inter-module symbol dependencies, and writes `modules.dep(.bin)` in a fixed
byte-order index format; it never *executes* module code. The cross-arch failure exists only
because libguestfs `command()` chroots into the guest and runs the *guest's* `depmod` binary.
Running the **host's** `depmod` against the extracted module tree with `-b <basedir>` indexes a
ppc64le tree correctly under an x86_64 host.

`_extract_and_index` is restructured to index host-side and inject the result, replacing the
in-guest `depmod` for **every** arch (not an arch-conditional branch — one path, arch-neutral,
per replace-don't-deprecate):

1. Extract `modules_tar` to a host temp dir (it already holds `lib/modules/<version>/…`).
2. Run the host `depmod -b <tmpdir> <version>` (`subprocess`, arch-neutral). Missing host
   `depmod` → `MISSING_DEPENDENCY` (named, actionable — same class as the missing-libguestfs
   guard); a non-zero exit or absent `modules.dep` → `INFRASTRUCTURE_FAILURE` carrying the
   trimmed stderr in `details` (see §2a).
3. `tar_in` the now-indexed tree into the guest overlay (the file steps libguestfs *can* do
   cross-arch — file I/O, not execution), then assert `modules.dep` present in-guest (the
   existing post-condition, unchanged).

The rm-then-extract clobber (`rm_rf(version_dir)` before `tar_in`) is preserved so a retried
install self-heals a partial tree. The host temp dir is cleaned up in a `finally`.

**Why host-side over the alternatives** (fully argued in ADR-0346):
- *qemu-user + binfmt in the appliance* — needs a custom libguestfs appliance carrying
  `qemu-ppc64le-static` and a `binfmt_misc` registration; heavy, host-global, and reused by
  nothing else. ADR-0345 rejected the same qemu-user route for customization.
- *arch-conditional (guest depmod when native, host depmod when foreign)* — two code paths to
  maintain and test for no benefit; host `depmod` is correct for the native case too.

#### 2a. Diagnosability (the #1146 note)

#1146 flagged that `_extract_and_index` collapses the failure into an `INFRASTRUCTURE_FAILURE`
whose `details` carry only the exception *type name*, so the real cause (`Exec format error`)
survived only on `__cause__` in the worker log. Host-side `depmod` removes that *specific*
failure, but the new subprocess path applies the lesson: a non-zero `depmod` exit surfaces the
**trimmed stderr substring** in the categorized `details`, so a future indexing failure is
legible from the tool envelope without log-diving.

### 3. pseries VMCOREINFO/fw_cfg — empirical verdict, recorded (not assumed)

**Hypothesis (to be proven or corrected live):** a ppc64le **kdump** capture needs no
`<features>` device emission on pseries, so the x86-only `emit_acpi_features` gate is already
correct and no code change is required. Rationale: `<features><acpi/><vmcoreinfo state="on"/>`
makes **QEMU** write a VMCOREINFO note into a **host-initiated** dump (`_append_crash_capture_features`'s
own docstring: "needed for host_dump capture" — the HOST_DUMP/`virsh dump` path). kdump instead
captures via the guest's *own* crash kernel: makedumpfile reads VMCOREINFO from the crashed
kernel's `/proc/vmcore` ELF note, which the crash kernel exports independently of any QEMU
fw_cfg device. So the QEMU vmcoreinfo device is orthogonal to the kdump path on either arch.

The verdict is **decided by the live proof** (§4), not by this prose:
- **If** the ppc64le kdump vmcore is captured and makedumpfile reads valid VMCOREINFO with **no**
  `<features>` emitted → the hypothesis holds; ADR-0346 records "kdump on pseries needs no
  `<features>` VMCOREINFO/fw_cfg device; the x86-only gate is correct; the pseries host_dump
  fw_cfg/device-tree question is a separate capture method, out of scope here." No code change.
- **If** capture fails for want of a pseries `<features>`/device emission → `emit_acpi_features`
  becomes a finer per-arch/per-capture-method trait and the pseries device (device-tree-located
  vmcoreinfo, **no** acpi — pseries has none) is emitted, with a test and the ADR recording the
  correction.

Either branch retires the epic's "pseries fw_cfg/VMCOREINFO device behavior (issue 9)"
Known-unverified item with a reproduced fact.

### 4. Live proof: force-crash a ppc64le guest → kdump → retrieve vmcore (blocking AC)

A documented `live_stack` run on the x86_64 host, driven over the real MCP HTTP spine (mirroring
#1146's driver and proof-record format), skipping cleanly without `qemu-system-ppc64` /
`KDIVE_GUEST_IMAGE_PPC64LE`:

1. `allocate → provision(arch=ppc64le)` — admission persists `accel=tcg`; the pseries/qemu domain
   boots the baseline rootfs to `ready`.
2. Package the guest's own kdump-capable baseline ppc64le kernel + its `lib/modules/<ver>/` as an
   ADR-0343 combined tar (`boot/vmlinuz` = the ELF, `lib/modules/<ver>/`) plus the matching
   `initrd`, sourced from the Fedora ppc64le scaffold #1144/#1146 already publish — no
   cross-compile toolchain. Upload both; `runs.complete_build`.
3. `runs.install` **with the KDUMP method** (a profile `crashkernel` opt-in, or the per-install
   `crashkernel` argument) → this **fires the module injection §2 unblocks**. Assert the install
   step succeeds (the depmod fix is exercised end-to-end here: the ppc64le module tree is indexed
   host-side and injected; `/lib/modules/<ver>/modules.dep` is present in-guest). Boot with the
   **default** ppc64le crashkernel (512M) — the §1 default is live-exercised, not just unit-tested.
4. `control.force_crash` (sysrq-c via the destructive-op gate) → the guest panics → its kdump
   kernel kexec-boots, runs makedumpfile, writes the vmcore → kdive's existing capture job
   harvests it (`capture_vmcore`, #115/#654).
5. `jobs.wait` the capture → the vmcore artifact is retrieved through the existing pipeline;
   **record the makedumpfile-reported fields** (the AC: "makedumpfile fields recorded"). Assert
   the retrieved core is a ppc64le ELF (`EM_PPC64`) and non-empty.

**Discriminating attribution** (not a bare "capture succeeded"): the proof records the running
domain's `<kernel>` at the per-Run staged path, a unique install cmdline token in the crashed
guest's console, `crashkernel=512M` in the guest `/proc/cmdline` (the §1 default reached the
kernel), and the vmcore's `EM_PPC64` machine — so the captured core is provably *this* ppc64le
guest's, under the default reservation, via the install-plane KDUMP path.

**Falsifiable pre-registered signals** (named before the run, mirroring #1146): capture success =
a non-empty `EM_PPC64` vmcore artifact retrieved **and** makedumpfile field output recorded. A
capture failure records the pre-registered failure mode (crashkernel too small → kdump kernel OOM
/ no `/proc/vmcore`; missing modules → capture kernel cannot mount the dump target; depmod-fix
regression → install step fails at indexing) and is **iterated to a definitive verdict**, not
shipped as "indeterminate" — the user requires real capture proof (no CONSTRAINED fallback for
the capture itself).

The console record, the makedumpfile fields, the crashkernel-default confirmation, and the
VMCOREINFO/fw_cfg verdict (§3) are written to
`docs/design/2026-07-13-ppc64le-kdump-proof-record-1148.md`.

## Acceptance criteria

1. **Per-arch crashkernel default, x86 unchanged.** `system_required_cmdline` emits
   `crashkernel=256M` for x86_64 (byte-identical to today) and `crashkernel=512M` for ppc64le on
   the KDUMP path when no explicit reservation is given; an explicit ADR-0300 reservation still
   overrides on both arches. Asserted by arch-parameterized unit tests; `DEFAULT_CRASHKERNEL` is
   removed and no reader references a hardcoded 256M for the default.
2. **Cross-arch module injection works.** `_extract_and_index` indexes the module tree host-side
   (`depmod -b`), so a ppc64le KDUMP/debuginfo install completes under the x86_64 libguestfs
   appliance; a non-zero `depmod` surfaces its stderr in the categorized `details`. x86_64
   injection behavior is preserved (the same modules.dep post-condition holds). Unit-tested with
   the host-subprocess seam faked; live-exercised in the §4 proof.
3. **Agent-facing text is arch-accurate and single-sourced.** The `runs.install` `crashkernel`
   `Field`/docstring name the per-arch defaults derived from the trait table, not a hardcoded
   256M; `runs.get data.required_cmdline` surfaces the effective per-arch default.
4. **pseries VMCOREINFO/fw_cfg verdict recorded.** ADR-0346 records, as a reproduced fact from the
   live capture, whether a ppc64le kdump needs any `<features>` device emission, and adjusts
   `xml.py`/`arch_traits` only if the capture forces it (hypothesis: no change). The epic's
   "pseries fw_cfg/VMCOREINFO device behavior (issue 9)" item is retired.
5. **Live capture recorded (blocking, discriminating).** A documented `live_stack` run
   force-crashes a ppc64le guest under TCG on the x86_64 host, its kdump kernel captures a vmcore,
   and the vmcore is retrieved through the existing pipeline with makedumpfile fields recorded;
   the proof attributes the core to the install-plane KDUMP path at the default 512M reservation
   (per-Run staged `<kernel>`, `crashkernel=512M` in `/proc/cmdline`, `EM_PPC64` core).

## Scope / non-goals

- **No new persistence / migration.** The crashkernel default flows through cmdline composition;
  no schema change (`installed_crashkernel` semantics — records only a client override — are
  unchanged).
- **No host_dump-on-pseries work.** The QEMU fw_cfg/device-tree VMCOREINFO device for the
  HOST_DUMP capture method is a separate capture path; §3 records why it is orthogonal to kdump
  and defers it.
- **fadump is out of scope** (epic issue 12) — kdump is the spine here.
- **No cross-compile toolchain.** The proof repackages the guest's own baseline ppc64le kernel.
- **No gdb/drgn on the captured core** (issues 10/11) — this issue captures + retrieves + records
  makedumpfile fields; typed introspection of the ppc64le core is the drgn sub-issue.
- **remote-libvirt is out of scope** (separate provider epic); its install seam is not the kernel
  writer.
- **No `live_vm_tcg` marker** (epic issue 15) and no big-endian ppc64 (epic non-goal).
