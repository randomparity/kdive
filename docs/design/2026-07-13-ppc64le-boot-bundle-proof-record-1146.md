# Proof record — live TCG boot of an *uploaded* ppc64le kernel bundle (#1146)

Date: 2026-07-13
Issue: #1146 · Epic: #1139 · Spec: `2026-07-13-ppc64le-boot-bundle-1146.md` · ADR-0344

The documented live proof required by #1146 AC #3/#4/#5: on the x86_64 host, an **uploaded**
ppc64le kernel bundle (packaged per the ADR-0343 contract) is installed into a provisioned
ppc64le System and **direct-kernel-boots on pseries under TCG**, reaching `runs.boot` readiness —
proving the install/boot path (`extract_boot_vmlinuz` → `<os>` render → SLOF boot) is arch-opaque
for ppc64le. Two verdicts the spec flagged as empirical are recorded here: the pseries
**initrd-addressing** finding and the guest-kernel-writer **cross-arch `depmod`** verdict.

## Environment

- Host: x86_64, `qemu-system-ppc64` present; libvirt advertises `ppc64le` as a bootable guest arch
  with `accel=tcg` (ADR-0338/0339). Live stack: `scripts/live-stack/up.sh` (Postgres + MinIO +
  mock-OIDC + server/worker/reconciler), build `g0876db972`.
- System rootfs: the published Fedora ppc64le scaffold
  `/var/lib/kdive/rootfs/local/fedora-kdive-ready-44-ppc64le.qcow2` — whole-disk ext4 (ADR-0272),
  `kdive-ready.service` enabled on `hvc0`, baseline kernel `6.19.10-300.fc44.ppc64le`. The scaffold
  build recipe (arch-safe file-op customization of the sha256-pinned Fedora ppc64le GenericCloud
  base, then `virt-tar-out` + `virt-make-fs` whole-disk ext4 repack) reproduces #1144 §4; the
  btrfs-subvolume base requires a `guestfish` rw tar-out (replays the btrfs log) before
  `virt-make-fs`.
- Uploaded bundle (the artifact under test): the ADR-0343 combined kernel tar — `boot/vmlinuz` =
  the **ELF64-LE `EM_PPC64`** `vmlinuz-6.19.10-300.fc44.ppc64le` (68 MB gzip), `lib/modules/<ver>/` —
  plus the separate `initrd` = `initramfs-6.19.10-300.fc44.ppc64le.img` (39 MB), extracted from the
  scaffold. The boot member's `e_machine` at offset `0x12` reads `21` (`EM_PPC64`), so it validates
  as a ppc64le ELF at `complete_build`, not a bzImage.

## Result 1 — uploaded ppc64le bundle direct-kernel-boots on pseries under TCG (PASS)

Driven over the live MCP HTTP spine (System `19766dea…`, Run `45dea414…`):

```
allocate → provision(arch=ppc64le) → systems.get accel = "tcg"     (admission persisted it, ADR-0339)
runs.create(build_profile={schema_version:1, arch:ppc64le})        (external-build, #1145)
artifacts.create_run_upload(kernel, initrd) → PUT kernel.tar.gz 200 · PUT initrd.img 200
runs.complete_build → build complete                               (validated the ELF EM_PPC64 boot member, ADR-0343)
runs.install(cmdline="kdive_proof_token=4170c77fc55b")             (CONSOLE method → plain boot, no injection)
runs.boot → BOOT REACHED READINESS                                 (uploaded ELF direct-kernel-booted on pseries/TCG)
```

The running domain (`virsh dumpxml kdive-19766dea…`) confirms the install plane produced the boot,
not pre-existing baseline state — the **discriminating attribution** (criterion 3):

```xml
<type arch='ppc64le' machine='pseries-10.2'>hvm</type>
<kernel>/var/lib/kdive/install/19766dea…/45dea414…/kernel</kernel>     ← per-Run staged path
<initrd>/var/lib/kdive/install/19766dea…/45dea414…/initrd</initrd>     ← per-Run staged path
<cmdline>console=hvc0 root=/dev/vda kdive_proof_token=4170c77fc55b</cmdline>   ← the unique proof token
<emulator>/usr/bin/qemu-system-ppc64</emulator>                        ← pseries TCG emulator
```

Proves, through the real admission→install→boot spine:

- `extract_boot_vmlinuz` extracted the **ELF** `boot/vmlinuz` and staged it byte-agnostically; the
  domain `<kernel>` boots it via QEMU/SLOF `-kernel` on `pseries` with no bzImage assumption.
- The `<kernel>`/`<initrd>` resolve to the **per-Run** staged path (`{system_id}/{run_id}/…`), and
  the unique `kdive_proof_token` reached the running kernel's cmdline — the boot is attributable to
  *this install*, not the #1144 baseline boot of the same bytes.

## Initrd-addressing verdict — NO ADDRESSING QUIRK (retires epic issue 7)

The Fedora ppc64le baseline kernel is modular, so the uploaded bundle boots with a staged
`<initrd>` (a no-initrd boot is not attempted). `runs.boot` **reached readiness**, which by the
ADR-0055 readiness contract means the **`kdive-ready` marker was observed on `hvc0`** — the
readiness classifier's only success signal. `kdive-ready` is emitted by a systemd unit that runs in
the **real root**, post-pivot (ADR-0342), so its appearance proves the staged initramfs unpacked,
mounted root from `/dev/vda`, and pivoted — exactly what an initrd-addressing failure would
prevent. No `Kernel panic … VFS: Unable to mount root fs` / `dracut` FATAL token appeared (the boot
succeeded). **pseries/SLOF direct-kernel boot needs no special `<initrd>` addressing beyond QEMU's
`-initrd`** (QEMU sets the device-tree initrd properties; the kernel reads them). No code
accommodation was required. This retires the epic's "SLOF direct-kernel boot … (issue 7)"
Known-unverified item.

## Result 2 — guest-kernel-writer cross-arch `depmod` verdict: CONSTRAINED (deferred to issue 9)

A second Run (`0269e688…`) uploaded a `vmlinux` (the ppc64le ELF, GNU build-id
`06466f96…`) as debuginfo, so `complete_build` set `debuginfo_ref` and `runs.install` fired
`_RealGuestKernelWriter.inject`, whose `_extract_and_index` runs the guest's **ppc64le** `depmod`
inside libguestfs's **x86_64** appliance. The install job failed; the worker log
(`.live-stack-logs/worker-root.log`) carries the chained cause:

```
guest.command(["depmod", "-a", version])  (guest_kernel_writer.py:135)
  → RuntimeError: command: depmod: Exec format error
  → CategorizedError: libguestfs failed extracting and indexing the kernel modules for kernel staging
```

This is **exactly the libguestfs same-arch `command` constraint** the spec/ADR anticipated: the
guest's ppc64le `depmod` ELF cannot execute in the x86_64 appliance without `qemu-user`+`binfmt`
(stock appliances carry neither). The verdict:

- The **plain-boot path (no injection) is unaffected** — Result 1 injects no modules and boots
  fine. Module injection fires only for KDUMP or a `debuginfo_ref` install.
- The writer's in-guest `depmod` on a ppc64le overlay is **CONSTRAINED, not verified**; the
  `qemu-user`/`binfmt` appliance accommodation (or a host-side `depmod`/modules.dep approach) is
  scoped to **issue 9 (kdump)**, where module injection is load-bearing. It is *not* marked
  UNVERIFIED — the constraint is a definitive, reproduced finding, captured in ADR-0344.
- Diagnosability note for issue 9: `_extract_and_index` collapses the failure to an
  `INFRASTRUCTURE_FAILURE` carrying only the exception *type name* (`guest_kernel_writer.py`
  134-137/192-198); the `depmod: Exec format error` cause survives only on `__cause__` in the
  worker log. Surfacing that substring in the categorized `details` would make a future cross-arch
  failure legible without log-diving.

## Verdict summary

| claim | verdict | evidence |
|-------|---------|----------|
| uploaded ppc64le ELF bundle installs + direct-kernel-boots on pseries/TCG | **PASS** | `runs.boot` readiness; `<kernel>` ELF at per-Run path |
| boot attributable to the install plane (not baseline) | **PASS** | per-Run `<kernel>`/`<initrd>` path + `kdive_proof_token` in `<cmdline>` |
| pseries initrd addressing needs an accommodation | **NO (no quirk)** | `kdive-ready` on `hvc0` (post-pivot); no VFS-mount panic — retires issue 7 |
| real writer's in-guest `depmod` works on a ppc64le overlay (x86_64 appliance) | **CONSTRAINED** | `depmod: Exec format error`; deferred to issue 9 |

Bundle build + driver scripts retained out of tree under `/home/dave/kdive-ppc-proof/`.
