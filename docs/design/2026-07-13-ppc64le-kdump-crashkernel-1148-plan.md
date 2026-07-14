# Implementation plan — kdump on ppc64le: per-arch crashkernel defaults + capture proof (#1148)

Spec: `docs/design/2026-07-13-ppc64le-kdump-crashkernel-1148.md` ·
ADR: `docs/adr/0346-ppc64le-kdump-crashkernel-defaults.md`

Branch: `feat/ppc64le-kdump-crashkernel-1148` · Base: `main` ·
**No migration, no schema change, no new config.** The crashkernel default flows through cmdline
composition; the depmod change is internal to the guest kernel writer.

TDD throughout: write the failing (or arch-parameterized) test first, then the code. Commit per
task with a conventional message ending in the repo's `Co-Authored-By` trailer. Keep guardrails
green at each commit — `just lint` (ruff), `just type` (ty, whole tree), `just test`; run
`just ci` before push. Task 1 touches a tool `Field`/docstring (the agent surface), so run
`just docs-check` after it (generated tool-doc drift). The live proof (Task 3) is gated behind the
`live_stack` marker, so `just test`/CI exclude it.

## Ground truth (verified this session)

- **The default fallback is one line.** `system_required_cmdline`
  (`src/kdive/services/runs/steps.py:343`) composes the platform cmdline; the KDUMP branch is
  `tokens.append(f"crashkernel={crashkernel or DEFAULT_CRASHKERNEL}")` (line 372).
  `DEFAULT_CRASHKERNEL = "256M"` (line 33). The `arch` is already a parameter, so the per-arch
  default is `crashkernel or arch_traits(arch).default_crashkernel`.
- **`DEFAULT_CRASHKERNEL`'s readers** (grep `DEFAULT_CRASHKERNEL`): (a) `steps.py:372` (the
  fallback); (b) `src/kdive/mcp/tools/lifecycle/runs/registrar.py:44` import + `:445`/`:451` in the
  `crashkernel` `Field` description (the agent contract) — an f-string embedding `256M`. No other
  reader. Removing the constant means updating both the fallback and the `Field` text.
- **`arch_traits` table** (`src/kdive/domain/platform/arch_traits.py`): frozen `ArchTraits`
  dataclass with `machine`/`console_device`/`pin_nic_slot`/`kvm_cpu_mode`/`emit_acpi_features`;
  `_TRAITS` has `x86_64` and `ppc64le` rows; `arch_traits(arch)` fails `CONFIGURATION_ERROR` on an
  unknown arch. Add `default_crashkernel: str` as a new field + a docstring `Attributes:` entry.
  Test file: `tests/domain/platform/test_arch_traits.py`.
- **`_required_cmdline`** (`src/kdive/mcp/tools/lifecycle/runs/view.py:117`) recomposes
  `system_required_cmdline(method, root_cmdline, arch=_system_arch(system))` with **no** explicit
  `crashkernel`, so `runs.get data.required_cmdline` surfaces the per-arch default automatically —
  no change needed there, but assert it in a test.
- **`step_progress` docstring** (`steps.py:209`) says "the default 256M was in force" — generalize
  to "the arch default was in force". `installed_crashkernel` records only a *client override*
  (`install.py:237` records `plan.crashkernel = payload.crashkernel`), so its semantics are
  unchanged; `None` still means "no per-install override" (the arch default applied).
- **The profile `crashkernel` token is a KDUMP *method signal*, not a reservation size.**
  `LocalLibvirtProfilePolicy.capture_method` (`providers/local_libvirt/profile_policy.py:41`)
  returns `KDUMP` iff `section.crashkernel is not None`; the token's *value* is never read as the
  size. Only the per-install ADR-0300 argument (`payload.crashkernel`) or the arch default sizes
  the cmdline (verified: no local reader sizes from `profile.provider.local_libvirt.crashkernel`).
  This is pre-existing and out of scope to change — but it is what lets the live proof (Task 3) set
  the profile token to a sentinel ≠ the default and observe the default win. (Note it in the proof
  record as a known quirk; do **not** "fix" it here.)
- **Module injection triggers** only on `request.method is CaptureMethod.KDUMP or
  request.debuginfo_ref is not None` (`providers/local_libvirt/lifecycle/install.py:339`).
  `_RealGuestKernelWriter._extract_and_index`
  (`providers/local_libvirt/lifecycle/boot/guest_kernel_writer.py:127`) is the target: today
  `rm_rf(version_dir)` → `tar_in(tar, "/")` → `guest.command(["depmod", "-a", version])` (line 133,
  the cross-arch failure) → assert in-guest `modules.dep`. The whole `_RealGuestKernelWriter` is
  `# pragma: no cover - live_vm` (needs libguestfs), so the host-side indexing must be factored
  into a **pure, guestfs-free helper** to be unit-testable (Task 2).
- **The kdump capture spine to mirror** (x86, in `tests/integration/test_live_stack.py` ~line
  414-430): `control.force_crash` (admin, gated) → `await_system_state("crashed")` →
  `vmcore.fetch` (the capture job) → `drain_job` → `vmcore.list` → refs. The redaction contract
  requires the surfaced ref be `-redacted` (raw `vmcore-<method>` must not leak).
- **ppc64le live harness exists:** `_ppc64le_reachability_preflight()` (line 815, gates on
  `qemu-system-ppc64` + `KDIVE_GUEST_IMAGE_PPC64LE` + stack/db), `_reachability_provision_profile(
  image, *, arch, crashkernel)` (line 702) — provisions **`memory_mb: 2048`** (satisfies the ≥2 GB
  precondition) and threads `crashkernel` into the profile section (the KDUMP method signal). The
  crash-capable x86 profile factory (force_crash opt-in) is at line 171.
- **Rootfs kdump-userspace gap:** the published ppc64le scaffold
  (`fedora-kdive-ready-44-ppc64le.qcow2`, #1144/#1146) is a minimal file-injection image; the live
  proof's precondition is a kdump-enabled ppc64le rootfs (kexec-tools + `kdump.service` + dracut
  kdump module — the x86 kdive-ready kdump config-fragment, PR#330). Preparing/confirming it is
  Task 3, step 0, and is the single biggest risk to the blocking proof.

## Tasks

### Task 1 — Per-arch crashkernel default in `arch_traits`; retire `DEFAULT_CRASHKERNEL`

**What / where the task fits:** Spec §1/§1a, ADR-0346 §1; criteria 1, 3. Move the crashkernel
default into the trait table so a ppc64le guest reserves 512M by default while x86 stays 256M and
the ADR-0300 per-install override still wins.

**Files:**
- `src/kdive/domain/platform/arch_traits.py` — add `default_crashkernel: str` to `ArchTraits`
  (+ `Attributes:` docstring entry) and to both `_TRAITS` rows (`x86_64="256M"`, `ppc64le="512M"`).
  Add a single-source helper `default_crashkernel_summary() -> str` returning a rendered
  per-arch summary from `_TRAITS` (e.g. `"256M on x86_64, 512M on ppc64le"`, sorted for stability)
  for the agent-facing text.
- `src/kdive/services/runs/steps.py` — change line 372 to
  `crashkernel or arch_traits(arch).default_crashkernel`; **remove** the module-level
  `DEFAULT_CRASHKERNEL` constant; update the `system_required_cmdline` docstring (which cites "the
  default `256M`") and the `step_progress` docstring ("the arch default was in force").
- `src/kdive/mcp/tools/lifecycle/runs/registrar.py` — drop the `DEFAULT_CRASHKERNEL` import;
  rebuild the `crashkernel` `Field` description to name the per-arch defaults via
  `default_crashkernel_summary()` (not a hardcoded `256M`): e.g. "reverts … to the platform
  per-arch default ({summary})".

**Do (tests first):**
1. `tests/domain/platform/test_arch_traits.py`: assert `arch_traits("x86_64").default_crashkernel
   == "256M"` and `arch_traits("ppc64le").default_crashkernel == "512M"`; assert
   `default_crashkernel_summary()` contains both arch:value pairs (so the agent text can't drift).
2. `tests/services/runs/test_cmdline.py`: **update** `test_ppc64le_leads_with_the_hvc0_console`
   (currently asserts `crashkernel=256M` for ppc64le — line 66-72) to expect `crashkernel=512M`;
   add an x86-unchanged assertion (`crashkernel=256M`) and an explicit-override-wins-on-ppc64le
   assertion (`crashkernel="384M"` → `crashkernel=384M`, default not applied). Keep all other x86
   assertions byte-identical.
3. A `runs.get`/view test (or extend an existing one) that `_required_cmdline` for a ppc64le KDUMP
   System surfaces `crashkernel=512M` — the agent-visible effective default.
4. Then implement; grep the tree to confirm no remaining `DEFAULT_CRASHKERNEL` reference or
   hardcoded-256M default-fallback.

**Acceptance:** `just lint`, `just type`, `just test`, `just docs-check` green (docs-check because
the `Field` text — the tool schema — changed). x86 cmdline byte-identical; ppc64le default is 512M;
explicit override wins on both arches; `DEFAULT_CRASHKERNEL` gone; agent `Field` text names the
per-arch defaults from the single-source summary. Migration: none.

**Rollback:** revert the commit; self-contained (no persistence/schema).

### Task 2 — Host-side `depmod` in the guest kernel writer (unblock cross-arch injection)

**What / where the task fits:** Spec §2/§2a, ADR-0346 §2; criterion 2. Replace the in-guest
`guest.command(["depmod", …])` — which runs the guest's ppc64le binary in the x86_64 appliance —
with an arch-neutral host-side `depmod -b`, for every arch.

**Files:** `src/kdive/providers/local_libvirt/lifecycle/boot/guest_kernel_writer.py`.

**Do (tests first — the point is to make this unit-testable):**
1. Factor a **pure, guestfs-free** module-level helper, e.g.
   `index_modules_tar(modules_tar: Path, version: str, *, workdir: Path, run_depmod: DepmodRunner)
   -> Path` that: extracts `modules_tar` under `workdir` (the tar holds `lib/modules/<version>/…`);
   runs `run_depmod(basedir=workdir, version=version)`; asserts `workdir/lib/modules/<version>/
   modules.dep` exists; re-tars the `lib/modules/` subtree (gzip) to `workdir/indexed.tar.gz` and
   returns it. `DepmodRunner` is a `Protocol`/callable seam; the real one is
   `_run_host_depmod(basedir, version)` doing `subprocess.run(["depmod", "-b", str(basedir),
   version], capture_output=True, text=True, check=False)` with:
   - `FileNotFoundError` (no host `depmod`) → `CategorizedError` `MISSING_DEPENDENCY` (named,
     actionable — mirror the missing-libguestfs guard).
   - non-zero returncode → `CategorizedError` `INFRASTRUCTURE_FAILURE` carrying the **trimmed
     stderr** (last ~500 chars) in `details["depmod_stderr"]` (the #1146 diagnosability note).
2. `_extract_and_index` becomes: `rm_rf(version_dir)`, `index_modules_tar(...)` in a `tempfile.
   TemporaryDirectory()` (cleaned in a `finally`), `tar_in(indexed_tar, "/", compress="gzip")`,
   then the existing in-guest `modules.dep` post-condition assert (unchanged). The guest binary is
   no longer executed.
3. New unit test `tests/providers/local_libvirt/lifecycle/boot/test_module_indexing.py`:
   - build a fake `modules_tar` with `lib/modules/6.19.10-300.fc44.ppc64le/foo.ko` + a fake
     `run_depmod` that writes `modules.dep` under the extracted tree; assert `index_modules_tar`
     returns a tar containing `modules.dep` for the ppc64le version (arch suffix intact) and the
     original `.ko`.
   - a `run_depmod` that raises `FileNotFoundError` → `MISSING_DEPENDENCY`; one returning non-zero
     (via a fake that raises the mapped error, or test `_run_host_depmod` with a stub subprocess) →
     `INFRASTRUCTURE_FAILURE` with the stderr substring in `details`.
   - the missing-`modules.dep`-after-depmod branch raises the categorized error.
   (These are pure host tests — no libguestfs; the `tar_in` step stays in the `# pragma: no cover -
   live_vm` real writer and is exercised by Task 3's live run.)

**Acceptance:** `just lint`, `just type`, `just test` green. `index_modules_tar` unit-tested for
the ppc64le version, the missing-`depmod` and non-zero-exit failure categories, and the
diagnosability substring. The in-guest `guest.command(["depmod", …])` is gone; x86 behavior is
preserved (same `modules.dep` post-condition). Live-exercised in Task 3.

**Rollback:** revert the commit; the writer reverts to in-guest depmod (x86-only capture), no
schema/persistence impact.

### Task 3 — Live proof: ppc64le kdump capture under TCG + proof record + ADR verdict (blocking)

**What / where the task fits:** Spec §3/§4, ADR-0346 §3/§4; criteria 4, 5. The headline
deliverable: a documented force-crash → kdump → retrieve of a ppc64le vmcore at the default 512M
reservation, and the pseries VMCOREINFO/fw_cfg verdict. Runs on this x86_64 dev host (KVM/libvirt
+ `qemu-system-ppc64`); **skips cleanly** without the ppc64le emulator/image/stack (the #1144
preflight idiom).

**Step 0 — precondition: a kdump-enabled ppc64le rootfs.** Confirm or prepare a ppc64le rootfs
with kexec-tools + `kdump.service` enabled + the dracut kdump module (mirror the x86 kdive-ready
kdump config-fragment, PR#330), published where `KDIVE_GUEST_IMAGE_PPC64LE` points. If the #1146
scaffold lacks it, extend the scaffold build recipe (arch-safe file-op injection of the kdump
config fragment + enabling the unit) — file operations are arch-safe under libguestfs; no guest
execution. **This is a hard precondition of the capture** — record in the proof that it was met.

**Files:**
- `tests/integration/test_live_stack.py` — a new `@pytest.mark.live_stack`
  `test_ppc64le_kdump_capture_over_the_wire`, reusing `_ppc64le_reachability_preflight()` and
  `_reachability_provision_profile(image, arch="ppc64le", crashkernel="256M")` (the **sentinel**
  profile token: KDUMP method on, sized ≠ the 512M default; `memory_mb` already 2048).
- `docs/design/2026-07-13-ppc64le-kdump-proof-record-1148.md` — the proof record (mirror
  `2026-07-13-ppc64le-boot-bundle-proof-record-1146.md`).
- `docs/adr/0346-ppc64le-kdump-crashkernel-defaults.md` — fill the "Live-proof outcome (pending)"
  section with the recorded facts and the §3 VMCOREINFO/fw_cfg verdict.
- **Only if the live capture forces it:** a narrowly-scoped pseries `<features>` accommodation in
  `src/kdive/providers/local_libvirt/lifecycle/xml.py` + `arch_traits` + a test (spec §3).

**Do — the run (read the upload/complete_build seam live to wire it, per #1146):**
1. Provision `arch=ppc64le` (KDUMP method via the sentinel profile `crashkernel="256M"`; **no
   per-install `crashkernel`** so the arch default 512M applies); admission persists `accel=tcg`;
   boot the kdump-enabled rootfs to `ready`. Record the guest's total RAM.
2. Package the guest's own baseline ppc64le kernel + `lib/modules/<ver>/` as the ADR-0343 combined
   tar + its `initramfs-<ver>.img` (`select_kernel_and_initrd`); upload both; `runs.complete_build`.
3. `runs.install` on the KDUMP System (no per-install crashkernel). **This fires module injection
   (Task 2 host-side depmod).** Assert the install step succeeds and `/lib/modules/<ver>/
   modules.dep` is present in-guest (over SSH) — the depmod fix, end-to-end.
4. Read the booted guest's `/proc/cmdline` over SSH: assert `crashkernel=512M` (not the `256M`
   sentinel) — proves the §1 arch default sized it. Assert the per-Run staged `<kernel>` via
   `virsh dumpxml` (attribution).
5. `control.force_crash` (admin, gated) → `await_system_state("crashed")` → `vmcore.fetch` →
   `drain_job` → `vmcore.list`. Assert a non-empty `-redacted` vmcore ref surfaced (raw
   `vmcore-<method>` must not leak). Retrieve it; assert it is an `EM_PPC64` ELF core; **record the
   makedumpfile-reported fields** (the AC).
6. **VMCOREINFO/fw_cfg verdict (§3):** with **no** `<features>` emitted on the pseries domain
   (confirm via `virsh dumpxml`), the capture + makedumpfile succeeding is the evidence that kdump
   on pseries needs no device — record it. If capture fails for want of a device, land the scoped
   `xml.py`/`arch_traits` accommodation + test and record the correction instead.

**Do — the record:** Write the proof record with the console evidence, the `crashkernel=512M`
confirmation, the recorded guest RAM, the host-side-depmod install success, the makedumpfile
fields, the `EM_PPC64` core, and the VMCOREINFO/fw_cfg verdict. Fill ADR-0346's Live-proof outcome
section.

**Blocking-proof rule (per issue owner):** there is **no CONSTRAINED fallback for the capture
itself**. A failed capture is diagnosed against the pre-registered failure modes (crashkernel too
small → kdump kernel OOM / no `/proc/vmcore`; missing kdump userspace → capture kernel cannot run;
depmod regression → install fails at indexing) and iterated to a definitive captured vmcore, not
shipped indeterminate. If a genuine environmental blocker is hit, surface it — do not mark the AC
done.

**Acceptance (criteria 4, 5):** The `live_stack` test passes on the dev host (or skips cleanly
elsewhere) and drives provision→build→install→force_crash→capture→retrieve of a ppc64le vmcore at
the default 512M; the proof record captures the makedumpfile fields, the `crashkernel=512M`
default, and the VMCOREINFO/fw_cfg verdict; ADR-0346's Live-proof outcome is filled. `just ci`
green (the `live_stack` test is marker-gated; Tasks 1-2 carry the CI-green unit gate).

**Rollback:** the `live_stack` test skips without the harness, so it cannot redden CI; the proof
doc and ADR fill are additive. A forced pseries `<features>` accommodation (if any) is a small,
separately revertable commit with its own test.

## Task ordering & prerequisites

1 → 2 → 3. Task 1 (default) and Task 2 (depmod) are independent code changes but both gate the
live proof: Task 3's install exercises Task 2's host-side depmod and boots at Task 1's default, so
both must land (and be CI-green) first. Task 3 requires the live host, the kdump-enabled ppc64le
rootfs (step 0), and is inherently iterative on the capture. No task depends on a migration,
schema, or config change. Tasks 1-2 are context-free implementer-ready; Task 3 is host-bound and
owner-blocking (real capture required).
