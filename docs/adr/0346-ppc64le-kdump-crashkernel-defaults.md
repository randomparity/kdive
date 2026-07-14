# ADR 0346 — kdump on ppc64le: per-arch crashkernel defaults, host-side module indexing, and the pseries VMCOREINFO verdict

- **Status:** Accepted
- **Date:** 2026-07-13
- **Issue:** #1148
- **Epic:** #1139 (full ppc64le support)
- **Builds on:** ADR-0300 (#989 per-install crashkernel reservation seam), ADR-0344 (#1146
  uploaded-bundle direct-kernel boot; the cross-arch `depmod` constraint it recorded and
  deferred here), ADR-0340 (accel-derived domain XML; the x86-only `<features>` gate),
  ADR-0341 (TCG deadline scaling), ADR-0207/0203 (guest kernel writer / module injection)

## Context

Kdump on a ppc64le guest under TCG on the x86_64 host is blocked by three things, only one of
which the issue title names.

1. **The crashkernel default is x86-sized.** `system_required_cmdline`
   (`services/runs/steps.py`) falls back to a module-level `DEFAULT_CRASHKERNEL = "256M"` for
   every arch when the ADR-0300 per-install reservation is not given. 256M is the x86_64 default;
   ppc64le distros reserve more — RHEL 10's `kdump-utils` floor is 384M for a 2–4 GB guest and
   512M for 4–16 GB (roughly double x86). A ppc64le guest booting `crashkernel=256M` risks a
   kdump kernel that OOMs before it runs makedumpfile, producing no vmcore.

2. **The kdump capture path is blocked cross-arch.** A KDUMP install fires module injection
   (`install.py:339`), whose `_RealGuestKernelWriter._extract_and_index` runs
   `guest.command(["depmod", "-a", <ver>])` inside libguestfs — the *guest's* ppc64le `depmod`
   ELF in libguestfs's **x86_64** appliance. #1146's proof reproduced `depmod: Exec format error`.
   Module injection is load-bearing for kdump (the guest's `kdumpctl` needs `/lib/modules/<ver>`
   and `/boot/vmlinuz-<ver>` to build and kexec-load the capture kernel), so a ppc64le kdump
   install cannot complete until this is fixed. ADR-0344 scoped the fix to this issue.

3. **The pseries VMCOREINFO/fw_cfg behavior is unverified.** The x86 domain emits
   `<features><acpi/><vmcoreinfo state="on"/></features>` (`xml.py`), gated x86-only by
   `arch_traits.emit_acpi_features`. `_append_crash_capture_features`'s own docstring scopes it to
   **host_dump** capture (QEMU writing a VMCOREINFO note into a host-initiated `virsh dump` core).
   Whether a ppc64le *kdump* capture needs any `<features>` emission was left open by PR #1070 and
   flagged in the epic's "Known unverified" list (issue 9).

## Decision

### 1. The per-arch crashkernel default lives in `arch_traits`

`ArchTraits` gains `default_crashkernel: str`. `system_required_cmdline` resolves the fallback
from the arch (`crashkernel or arch_traits(arch).default_crashkernel`) instead of the module
constant, which is **removed** (replace-don't-deprecate).

- `x86_64 = "256M"` — value unchanged; the x86 cmdline is byte-identical to today.
- `ppc64le = "512M"` — clears RHEL's 384M floor for the smallest supported guest (2–4 GB) with
  margin and matches the 4–16 GB reservation. Single-valued to match the existing x86 default's
  shape; an operator wanting a range passes it explicitly through the ADR-0300 seam.

The ADR-0300 tunable seam is preserved and "profile/policy overrides still win" holds by
construction: an explicit per-install `crashkernel` is still `… or …`-preferred over the arch
default; only the `None` fallback became arch-keyed. No new persistence, no migration — the
default flows through cmdline composition. The agent-facing `runs.install` `crashkernel`
`Field`/docstring name the per-arch defaults **derived from the trait table** (a single-source
summary, not a re-typed literal), so the tool contract cannot drift; `runs.get`'s
`data.required_cmdline` already recomposes the cmdline and surfaces the effective per-arch
default with no change.

### 2. Module indexing moves host-side (`depmod -b`), for every arch

`_extract_and_index` extracts the module tar to a host temp dir, runs the **host** `depmod -b
<tmpdir> <version>`, and `tar_in`s the indexed tree into the guest overlay — replacing the
in-guest `guest.command(["depmod", …])` for **every** arch, not a foreign-only branch.

`depmod` is arch-neutral as a *tool*: it parses each module's ELF header/`.modinfo`, resolves
symbol dependencies, and writes `modules.dep(.bin)` in a fixed byte-order index format; it never
*executes* module code. So the host's x86_64 `depmod` indexes a ppc64le tree correctly. libguestfs
does the file I/O (`tar_in` — a cross-arch-safe file operation), and no guest binary is executed.
Missing host `depmod` → `MISSING_DEPENDENCY`; a non-zero exit or absent `modules.dep` →
`INFRASTRUCTURE_FAILURE` carrying the trimmed `depmod` stderr in `details` (the #1146
diagnosability note: the cause is legible from the tool envelope, not only the worker log). The
rm-then-extract clobber and the in-guest `modules.dep` post-condition are preserved; the host
temp dir is removed in a `finally`.

### 3. pseries VMCOREINFO/fw_cfg — verdict decided by the live capture, recorded here

**Hypothesis:** a ppc64le kdump capture needs no `<features>` device emission; the x86-only
`emit_acpi_features` gate is already correct. kdump captures via the guest's own crash kernel,
which exports VMCOREINFO in `/proc/vmcore`'s ELF note; makedumpfile reads it there, independent of
any QEMU fw_cfg vmcoreinfo device (that device serves the HOST_DUMP path). The verdict is decided
by the live capture (§Live-proof outcome), not by this reasoning — if capture succeeds with no
`<features>` and makedumpfile reads valid VMCOREINFO, no code change lands; if capture requires a
pseries device, `emit_acpi_features` is refined and a device-tree-located `<vmcoreinfo>` (no acpi
on pseries) is emitted with a test. Either way the epic's "pseries fw_cfg/VMCOREINFO device
behavior (issue 9)" item is retired with a reproduced fact.

### 4. Live capture proof (blocking)

A documented `live_stack` run force-crashes a provisioned ppc64le guest under TCG on the x86_64
host at the **default** 512M reservation; its kdump kernel captures a vmcore; the vmcore is
retrieved through the existing pipeline (`capture_vmcore`) and its makedumpfile fields recorded.

Named preconditions the proof establishes first (not assumed): a **kdump-enabled ppc64le rootfs**
(kexec-tools + `kdump.service` + dracut kdump module — the capture is guest-driven, and #1146's
scaffold has none) and **≥2 GB guest RAM** (a 512M reservation is honored only above the distro
threshold and must leave a bootable first kernel). KDUMP is opted into via the **profile**
`crashkernel` token set to a sentinel *different* from the arch default (e.g. `256M`) with **no
per-install argument** — the profile token is only a method signal, never the size — so observing
`crashkernel=512M` (not `256M`) in the guest `/proc/cmdline` proves the *arch default*, not the
profile/per-install value, sized the reservation. Discriminating attribution: that
`/proc/cmdline`, the per-Run staged `<kernel>`, the recorded guest RAM, and an `EM_PPC64` core
prove the capture is *this* ppc64le guest's under the §1 default via the §2-unblocked install
path. Per the issue owner, there is **no CONSTRAINED fallback for the capture itself** — a failed
capture is iterated to a definitive verdict, not shipped as indeterminate.

## Security note — the ELF-parsing trust boundary moves from the appliance to host root

Host-side indexing is a deliberate widening of the worker's host trust boundary, recorded here so
the tradeoff is reviewable. Before this change, the uploaded (authenticated, semi-trusted) module
tree was materialized and its ELF objects parsed **only inside the disposable libguestfs
appliance** (`guest.tar_in` + in-guest `depmod`) — an isolated, throwaway VM. Now the tar is
extracted to the worker host filesystem and the host's `depmod`/libkmod ELF+symbol parser runs over
it directly, in the worker's context. A libkmod parser flaw triggered by a crafted `.ko` would
therefore run on the host rather than being confined to the appliance.

Residual risk is bounded and accepted for this issue:

- The uploaded tar is authenticated and project-scoped (not anonymous input).
- Extraction keeps `data`-filter protection (no absolute names, no `..`, no symlink-escape write),
  rejects a path-traversal link as `CONFIGURATION_ERROR`, and is capped by cumulative uncompressed
  size (2 GiB) and member count (200k), so a tar/gzip bomb cannot exhaust the host temp filesystem.
- `depmod -b <tmpdir>` needs no elevated privilege; it only reads the temp tree and writes its
  index files there. The worker runs as root for pre-existing reasons (libvirt, libguestfs,
  staging under `/var/lib/kdive`), so this path inherits root but does not *require* it.

**Deferred hardening (not done here):** running the extraction + `depmod` as a dropped-privilege
user would shrink the exposure to non-root. That is a worker-wide privilege-model change (the
whole worker is root today), so it is out of scope for this sub-issue and left as a follow-up
rather than a one-off asymmetry.

## Live-proof outcome (2026-07-14)

Recorded in `docs/design/2026-07-13-ppc64le-kdump-proof-record-1148.md` (driver
`test_ppc64le_kdump_captures_a_vmcore_under_tcg`, `1 passed in 218.58s`, on this branch's build):

- **Per-arch default: PASS.** A ppc64le KDUMP install (sentinel profile `crashkernel="256M"`, no
  per-install override) resolved `console=hvc0 root=/dev/vda crashkernel=512M …` — the arch default
  (512M), not the sentinel, sized the reservation; the same reached the running domain's
  `<cmdline>`.
- **Host-side depmod: PASS.** The KDUMP install's module injection completed under the x86_64
  libguestfs appliance with no `Exec format error`, retiring #1146's CONSTRAINED verdict. The live
  run also caught and fixed a real defect the unit fakes missed — `extractall(filter="data")`
  rejected the absolute `build`/`source` symlinks every module tree carries; the fix skips those
  link members with a `data`-safe custom filter (regression test added).
- **Capture: PASS.** `force_crash` → `crashed` → the guest's kdump kernel + makedumpfile produced
  an ~86 MiB core, retrieved via `vmcore.fetch`/`vmcore.list` (only the `-redacted` ref surfaced).
  The makedumpfile KDUMP header reports `machine=ppc64le`, `release=6.19.10-300.fc44.ppc64le`
  (makedumpfile 1.7.9) — the discriminating attribution.
- **VMCOREINFO/fw_cfg verdict: NO device needed.** The capture succeeded with **no `<features>`
  device** emitted on the pseries domain (asserted absent in `virsh dumpxml`). kdump read VMCOREINFO
  from `/proc/vmcore`, independent of any QEMU device — the hypothesis held, so `xml.py`/`arch_traits`
  are unchanged and the x86-only `emit_acpi_features` gate is correct for the kdump path. The
  epic's issue-9 "pseries fw_cfg/VMCOREINFO device behavior" Known-unverified item is retired.

## Consequences

- A ppc64le guest reserves an arch-appropriate 512M for kdump by default; x86_64 stays 256M,
  byte-identical. An explicit ADR-0300 reservation overrides on either arch.
- Module injection is arch-neutral: a ppc64le KDUMP/debuginfo install completes under the x86_64
  libguestfs appliance. x86_64 injection is behavior-preserving (same modules.dep result). A
  future indexing failure surfaces `depmod` stderr in the tool envelope.
- One module-indexing path for all arches — no arch-conditional branch to drift.
- The pseries kdump VMCOREINFO/fw_cfg behavior is a documented, reproduced fact (§Live-proof
  outcome), not an assumption; the epic's issue-9 Known-unverified item is retired.
- No migration, no schema change. `xml.py`/`arch_traits` `<features>` emission changes only if
  the live capture forces it (hypothesis: no change).

## Rejected alternatives

- **Keep a single `DEFAULT_CRASHKERNEL` and let operators override per-install on ppc64le.**
  Rejected: the default must be *correct per arch* — an agent that does not pass a reservation
  (the common path) would silently get an undersized 256M on ppc64le and a capture that fails
  only at crash time. The arch is already known where the default is applied; keying it there is
  the fail-safe default.
- **A range-based ppc64le default (`2G-4G:384M,4G-16G:512M,…`).** Rejected for now: kdive's x86
  default is a single value, and a range couples the default to guest-memory assumptions the
  provisioner does not model here. 512M is a safe single value above the RHEL floor; an operator
  wanting a range passes it through the ADR-0300 seam. Revisit if a large-memory ppc64le guest
  wastes reservation.
- **qemu-user + `binfmt_misc` in the libguestfs appliance (run the guest's ppc64le `depmod`).**
  Rejected: needs a custom appliance carrying `qemu-ppc64le-static` and a host-global binfmt
  registration, reused by nothing else; ADR-0345 rejected the same qemu-user route for
  customization. Host-side `depmod` needs no appliance change and is arch-neutral.
- **Arch-conditional indexing (guest `depmod` when native, host `depmod` when foreign).**
  Rejected: two paths to maintain and test for no benefit — host `depmod -b` is correct for the
  native case too, so one path covers both.
- **Emit a pseries `<features><vmcoreinfo>` device pre-emptively for kdump.** Rejected as
  speculative: kdump does not use the QEMU vmcoreinfo device (§3). The emission changes only if
  the live capture proves it necessary — fail-fast on the evidence, ADR the finding either way.
- **Defer the live capture and assert the default/depmod fix unit-only.** Rejected: the AC
  requires a documented ppc64le vmcore captured and retrieved with makedumpfile fields, and the
  VMCOREINFO/fw_cfg verdict is only knowable by capturing — the exact unverified gap this issue
  closes. The issue owner requires real capture proof.

## Rollout

Additive and backward compatible. No migration; no behavior change on the x86_64 path (256M
default unchanged; host-side `depmod` produces the same modules.dep an in-guest `depmod` did).
ppc64le kdump is proven through the same capture/retrieve pipeline x86 already uses, unblocked by
the host-side indexing and sized by the per-arch default.
