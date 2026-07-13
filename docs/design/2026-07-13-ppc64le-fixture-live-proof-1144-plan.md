# Implementation plan — ppc64le profile fixture, seed row, live TCG boot proof (#1144)

Spec: `docs/design/2026-07-13-ppc64le-fixture-live-proof-1144.md` · ADR: `docs/adr/0342-ppc64le-live-tcg-boot-proof.md`

Branch: `feat/ppc64le-profile-fixture-1144` · Base: `main` · **No migration, no schema change**
(the fixture is file/embedded; the seed row is example inventory).

TDD throughout: write the failing test first, then the code. Commit per task with a
conventional message ending in the repo's `Co-Authored-By` trailer. Keep guardrails green at
each commit — `just lint` (ruff), `just type` (ty, whole tree), `just test`; run `just ci`
before push. Tasks 1–3 are unit-test-only and gate CI. Task 4 (the live boot) runs
out-of-band on this x86_64 host and is captured in a committed proof record; Task 5 folds its
findings back with unit tests.

## Ground truth (verified this session)

- This x86_64 host advertises `ppc64le` as a bootable guest arch with
  `emulator = /usr/bin/qemu-system-ppc64`, `accel = tcg` (`virsh capabilities`;
  ADR-0338 discovery). `qemu-system-ppc64` is installed.
- `arch_traits["ppc64le"]` already ships `machine="pseries"`, `console_device="hvc0"`,
  `pin_nic_slot=False`, `kvm_cpu_mode="host-model"`, `emit_acpi_features=False` (PR #1070 +
  #1142). These are the unverified defaults this issue falsifies.
- `fedora-kdive-ready-44-ppc64le` exists in `fixtures/local-libvirt/rootfs_catalog.toml`
  (build-fs catalog) with a sha256-pinned Fedora-secondary cloud-image URL.
- No ppc64le rootfs is published under `/var/lib/kdive/rootfs/local/` — the scaffold (Task 4)
  produces it.

## Existing shape (do not re-derive)

- **Fixture, two surfaces:** `fixtures/local-libvirt/profiles/console-ready_x86_64.yaml` +
  `fixtures/local-libvirt/manifest.yaml` `profiles:` list; and the embedded copy in
  `src/kdive/admin/default_fixtures.py` (`_PROFILE_RELATIVE`, `_PROFILE_YAML`, `_manifest_yaml()`
  profiles list, `LOCAL_LIBVIRT_FIXTURES`). Loaded by `components/catalog.py:load_fixture_catalog`.
- **Seed inventory:** `systems.toml.example` `[[image]]` blocks reconciled into `image_catalog`
  (ADR-0112). The x86_64 `fedora-kdive-ready-44` block (with an `[image.attested]` sub-block) is
  the template.
- **Readiness unit:** `images/families/_fedora_customize.py:129`
  `readiness_unit(kdump_unit, console_device)` — a pure standalone render that
  `ExecStart`s `echo kdive-ready > /dev/{console_device}`.
- **Build pipeline:** `providers/local_libvirt/rootfs_build.py:242` `build()` —
  acquire base → `_customize` (`virt-customize`, foreign-arch-unsafe) → `repack_whole_disk_ext4`
  (`virt-tar-out` + `virt-make-fs`, arch-safe, ADR-0272 layout).
- **Provision-time SSH key:** `providers/local_libvirt/lifecycle/rootfs/overlay_customize.py:24`
  `inject_authorized_key_argv` / `_real_inject_authorized_key` — `virt-customize --ssh-inject`,
  a libguestfs file write (arch-safe).
- **Live proof harness:** `tests/integration/test_live_stack.py:680`
  `test_family_guest_is_ssh_reachable_over_the_wire(family)` — allocate → provision → baseline
  kernel boots to `ready` → `ssh_info` → `authorize_ssh_key` drains *succeeded*. Per-family
  gating via `_FAMILY_IMAGE_ENV` + `_reachability_preflight`; profile via
  `_reachability_provision_profile(image)`. `live_stack`-marked.

**Tests to mirror:** `tests/admin/test_default_fixtures.py` (manifest + profile assertions),
`tests/mcp/catalog/test_fixtures_validate.py` (triple validation),
`tests/provider_components/test_catalog.py` and `test_default_fixture_catalog.py`
(`load_fixture_catalog` resolution). Seed-row parse: `tests/inventory/` (find the
`systems.toml`/`image_catalog` loader test that parses example-style blocks).

---

## Task 1 — ppc64le profile fixture across both surfaces + unit tests

**Where it fits:** spec §1. Gives an operator/live-test a ppc64le profile to point a System at,
kept identical across the file bundle and the embedded `install-fixtures` copy.

**Test first:**
- `tests/admin/test_default_fixtures.py`: extend `test_..._declare_manifest_and_profile` so the
  manifest `profiles` list equals
  `["profiles/console-ready_x86_64.yaml", "profiles/console-ready_ppc64le.yaml"]`; add a
  `test_console_ready_ppc64le_profile` asserting
  `LOCAL_LIBVIRT_FIXTURES["profiles/console-ready_ppc64le.yaml"]` parses to
  `{provider: local-libvirt, name: console-ready_ppc64le, arch: ppc64le}`.
- **Surface-parity test (spec AC#2):** a new test asserting the on-disk file bundle and the
  embedded `LOCAL_LIBVIRT_FIXTURES` agree for every key — read
  `fixtures/local-libvirt/manifest.yaml` + each `profiles/*.yaml` from disk and assert equality
  with the embedded dict, so the two copies cannot drift. (Colocate in `test_default_fixtures.py`.)
- `tests/mcp/catalog/test_fixtures_validate.py`: assert the resolved default catalog reports the
  ppc64le triple among its profiles.
- `tests/provider_components/test_catalog.py` / `test_default_fixture_catalog.py`:
  `load_fixture_catalog(DEFAULT_FIXTURE_CATALOG_PATH).profile("local-libvirt",
  "console-ready_ppc64le")` resolves with `arch == "ppc64le"`.

**Code:**
- Add `fixtures/local-libvirt/profiles/console-ready_ppc64le.yaml`
  (`provider: local-libvirt` / `name: console-ready_ppc64le` / `arch: ppc64le`).
- Append `- profiles/console-ready_ppc64le.yaml` to `fixtures/local-libvirt/manifest.yaml`.
- In `src/kdive/admin/default_fixtures.py`: add a `_PPC64LE_PROFILE_RELATIVE` +
  `_PPC64LE_PROFILE_YAML`, append the relative path to the `_manifest_yaml()` `profiles` list,
  and add the profile to `_build_fixture_files()`. Keep the x86_64 entries.

**Acceptance:** all listed tests pass; `load_fixture_catalog` resolves both profiles; the
surface-parity test passes. `just lint`/`just type`/`just test` green.

**Notes:** the manifest `profiles` order is asserted — update the x86_64 assertion to the
two-element list, don't leave it pinned to one. No `requires` block (ADR-0316/0319).

---

## Task 2 — `fedora-kdive-ready-44-ppc64le` seed baseline row + test

**Where it fits:** spec §2. The operator seed template gains the ppc64le image so a System can
resolve it; `image_catalog` rows reconcile from `systems.toml` (ADR-0112).

**Test first:** find the test that parses example/baseline `[[image]]` blocks into
`image_catalog` rows (grep `_BASELINE_SYSTEMS_TOML` in `tests/admin/test_bootstrap.py`, and the
inventory loader tests under `tests/inventory/`). Add a ppc64le `[[image]]` block to that test's
fixture TOML (or a dedicated small fixture) and assert it parses to a row with `arch ==
"ppc64le"`, `provider == "local-libvirt"`, the `s3` object_key, and the attested operands.
If a test validates `systems.toml.example` itself end-to-end (a "example parses" guard), it will
now exercise the new block — confirm it stays green.

**Code:** add to `systems.toml.example`, after the `fedora-kdive-ready-44` block, an
`[[image]]` for `fedora-kdive-ready-44-ppc64le`: `arch = "ppc64le"`, `format = "qcow2"`,
`root_device = "/dev/vda"`, `visibility = "public"`,
`capabilities = ["ssh", "selinux", "kdump", "drgn"]`, `[image.source]` `kind = "s3"`
`object_key = "rootfs/local/fedora-kdive-ready-44-ppc64le.qcow2"`, and `[image.attested]`
`boot_kernel_count = 1`, `makedumpfile_version = "1.7.9"`. A comment notes it is the
Fedora-secondary ppc64le sibling and that the attested operands describe the eventual #8-built
image, not the Task-4 scaffold.

**Acceptance:** the parse test passes; any `systems.toml.example`-validates guard stays green;
`just test` green.

---

## Task 3 — ppc64le case in the live-proof spine + the non-gated profile unit test

**Where it fits:** spec §5. Reuses the one proven real-provision→boot→SSH vehicle
(`test_live_stack.py`) rather than inventing a new harness. The ppc64le case exercises the whole
ADR-0339/0340/0341 chain by construction: admission persists `accel="tcg"`, the provider renders
a pseries/qemu/emulator domain, the boot handler applies the TCG-scaled deadline, and reaching
`ready` proves the readiness marker landed on the configured console (`hvc0`).

**Marker note (spec §5):** the vehicle is `live_stack` (the repo's only end-to-end
provision+boot path). This is a live-VM-class proof under TCG; the distinct `live_vm`/
`live_vm_tcg` marker split is issue 15's scope. Document this in the test docstring so the
marker choice is not read as a deviation.

**Non-gated unit test (gates CI):** mirror
`test_provision_profile_disk_gb_equals_allocation_request` for the ppc64le profile factory — a
plain unit test (no stack) asserting the ppc64le reachability profile's `disk_gb` equals
`LOCAL_ALLOCATION_DISK_GB` and `arch == "ppc64le"`. This keeps a CI-gating assertion on the new
factory even though the boot itself is `live_stack`-gated.

**Code (`tests/integration/test_live_stack.py`):**
- Add a ppc64le entry to the per-family image env map (`_FAMILY_IMAGE_ENV`) or a dedicated
  `KDIVE_PPC64LE_READY_IMAGE` env, and a `_reachability_preflight`-style skip that also skips
  when `qemu-system-ppc64` is absent (`shutil.which`).
- Add `_ppc64le_reachability_provision_profile(image)` mirroring
  `_reachability_provision_profile` but `arch = "ppc64le"`. **Open question to resolve in
  build:** the x86 factory sets `kernel_source_ref` from `_KERNEL_TREE_ENV` though the baseline
  kernel boots (not the referenced tree). Confirm whether `kernel_source_ref` is a
  used-for-boot field or a schema-only requirement; if schema-only, the ppc64le factory may
  reuse/omit it (a ppc64le System must **not** boot an x86 kernel). If it is used, the proof
  needs a ppc64le kernel tree — flag and decide before running Task 4.
- Add `test_ppc64le_guest_is_ssh_reachable_over_the_wire` (or extend the `parametrize` to
  include `ppc64le` if the profile/image plumbing generalizes cleanly): allocate → provision
  the `console-ready_ppc64le`/`fedora-kdive-ready-44-ppc64le` pairing → `await_system_state(...,
  "ready")` → `ssh_info` `worker_loopback` endpoint → `authorize_ssh_key` drains *succeeded*.

**Acceptance:** the non-gated profile unit test passes in `just test`; the new `live_stack`
test is collected and **skips cleanly** without the stack / `qemu-system-ppc64` / the published
rootfs. `just ci` green.

---

## Task 4 — Execute the live TCG boot proof + commit the proof record

**Not TDD — an out-of-band live run on this x86_64 host.** Produces the AC's documented boot.

**Steps (each captured verbatim in the proof record for reproducibility):**
1. **Scaffold the rootfs** (spec §4, arch-safe): acquire the sha256-pinned Fedora ppc64le
   GenericCloud qcow2; file-inject the `readiness_unit(kdump_unit, "hvc0")` systemd unit + its
   enable symlink via `guestfish`/libguestfs (no guest-code execution); run
   `virt-make-fs`/`virt-tar-out` `repack_whole_disk_ext4` to the ADR-0272 bootloader-less
   whole-disk ext4 layout; publish as `/var/lib/kdive/rootfs/local/fedora-kdive-ready-44-ppc64le.qcow2`.
2. **Bring up the stack** (`just stack-up` / the live-stack runbook), reconcile the ppc64le seed
   row, and export the ppc64le image env var the Task-3 test reads.
3. **Run the proof:** `just test-live-stack` filtered to the ppc64le reachability test (allow the
   full TCG-scaled deadline; a ppc64le TCG boot is ~10× an x86 KVM boot).
4. **Capture** the console tail (showing the `hvc0` `kdive-ready` marker), the resolved
   `systems.get` `accel == "tcg"` + emulator, the `ssh_info` endpoint, and the drained
   `authorize_ssh_key` success into
   `docs/design/2026-07-13-ppc64le-tcg-boot-proof-record-1144.md`.

**Acceptance:** the ppc64le reachability test passes live; the proof record is committed with the
evidence and the exact scaffold commands. If the boot fails, it is a "pseries surprise" → Task 5.

**Rollback/cleanup:** the scaffold qcow2 and any provisioned domain/overlay are reclaimed by the
normal `release`/reconciler path; the proof record documents teardown.

---

## Task 5 — Fold pseries surprises into `arch_traits` (with tests) or retire the "unverified" language

**Where it fits:** spec §7. The boot is a falsification gate; this task records its verdict.

**If the boot confirms the defaults** (`pin_nic_slot=False` reachable, marker on `hvc0`, no ISA
SIGILL): edit the `arch_traits` docstring (and the epic design doc `docs/design/2026-07-13-ppc64le-full-support.md`
§Known-unverified bullet 4) to drop "needs live validation"/"unverified" and cite this proof
record. No behavior change.

**If a surprise surfaces** (NIC needs a pinned slot; marker lands off `hvc0`; ISA SIGILL):
- **Test first:** add a `tests/domain/platform/test_arch_traits.py` assertion pinning the
  corrected field (e.g. `arch_traits("ppc64le").pin_nic_slot is True`), and, where the fix
  changes rendered XML, a `tests/providers/local_libvirt/test_provisioning.py` render assertion.
- **Code:** apply the minimal `arch_traits` (or renderer) correction. An ISA/CPU correction that
  needs a rendered-`<cpu>`-for-TCG change is raised as a **follow-up issue against ADR-0340's
  "no `<cpu>` for TCG" decision** (not a silent pin here), per spec §7 and ADR-0342.
- Re-run the live proof to confirm the fix, and update the proof record.

**Acceptance:** `arch_traits` reflects reality (confirmed defaults documented, or corrected with
tests); no "unverified" language remains for the boot-proven facts; `just ci` green.

---

## Final verification (before PR)

- `just ci` green (lint, type, lint-shell, lint-workflows, check-mermaid, test).
- The two fixture surfaces agree (Task 1 parity test) and `load_fixture_catalog` resolves both
  profiles.
- `systems.toml.example` parses with the ppc64le row present.
- The proof record is committed with real evidence (console marker on `hvc0`, `accel="tcg"`,
  SSH-reachable drain success) — not a placeholder.
- `git grep -n "needs live validation\|unverified"` over `arch_traits.py` and the epic design
  doc reflects the boot verdict (removed if confirmed; unchanged only for still-unproven facts
  like fadump/kdump owned by later issues).
