# Plan — `debian` rootfs FamilyCustomizer + Debian 12/13 entries (#824)

- **Issue:** [#824](https://github.com/randomparity/kdive/issues/824) (epic #822, ADR-0251)
- **Spec:** [`../specs/2026-06-25-local-multidistro-rootfs-catalog-817.md`](../specs/2026-06-25-local-multidistro-rootfs-catalog-817.md) — `## Follow-up realization: #824`
- **Execution:** tasks are tightly coupled (shared-seam refactor + new family + provenance); implemented
  directly in one session with TDD, not subagent-dispatched.

Guardrails before every commit: `just lint`, `just type`, plus the focused tests for touched modules;
full `just test` + doc guards (`just docs-links docs-paths adr-status-check docs-check`) before the
first push.

## Task 1 — Generalize the shared readiness-unit + debug-helper seam

**Where it fits:** Debian's kdump unit is `kdump-tools.service`; the readiness `After=` edge (ADR-0251
point 6) must name the real unit per family. Debian needs the `kdive-drgn` helper staged but not the
NetworkManager keyfile.

**Files:** `src/kdive/images/families/_fedora_customize.py`,
`src/kdive/images/families/base.py`, `src/kdive/providers/local_libvirt/rootfs_build.py`,
`tests/images/families/test_fedora_customize.py` (+ new readiness-unit test).

**Changes:**
- Replace the `READINESS_UNIT` constant with `readiness_unit(kdump_unit: str) -> str` rendering
  `After=dev-ttyS0.device <kdump_unit>`. `rootfs_build._customize` calls
  `readiness_unit(family.kdump_unit)`.
- Split `debug_image_args` internally into `drgn_helper_args(cleanup)` (upload helper + chmod 0755,
  fail-loud if the reviewed helper is absent) and `ssh_nic_keyfile_args(cleanup)` (the NM keyfile);
  `debug_image_args` = both, gated on `"drgn" in packages` (rhel behavior unchanged). Export
  `drgn_helper_args` for the debian family.
- Add `kdump_unit: str` and `guest_mac: str` to the `FamilyCustomizer` protocol (`base.py`).

**Acceptance:** rhel argv output byte-identical (existing `test_rhel.py`/`test_rootfs_build.py` green);
`readiness_unit("kdump-tools.service")` contains `After=dev-ttyS0.device kdump-tools.service`.

## Task 2 — `DebianFamily` customizer + register it

**Where it fits:** the core deliverable — apt, `kdump-tools.service`, `ssh.service`, `python3-drgn`,
AppArmor-no-relabel `normalize`.

**Files:** `src/kdive/images/families/debian.py` (new),
`src/kdive/images/families/__init__.py`, `tests/images/families/test_debian.py` (new).

**Changes — `DebianFamily`:**
- `family = "debian"`, `kdump_unit = "kdump-tools.service"`, `guest_mac = "apparmor"`.
- `packages(kind, distro, version)`: build → Debian toolchain (`gcc make bc bison flex libssl-dev
  libelf-dev libncurses-dev dwarves rsync git`); debug → `makedumpfile kdump-tools crash python3-drgn
  openssh-server`.
- `customize_argv(ctx)`: `--install` set; `systemctl enable ssh.service`; gated on `kdump-tools` in
  packages → `systemctl enable kdump-tools.service`, set `USE_KDUMP=1` in `/etc/default/kdump-tools`
  (sed-replace existing line or append), write the shared NMI sysctl; cloud-image only → write
  `/etc/cloud/cloud-init.disabled` + seed `/etc/machine-id`; stage `drgn_helper_args` when
  `ctx.kind == "debug"`; `--ssh-inject root:file:<key>`; upload + enable the kdive-ready unit. **No**
  SELinux edit, **no** NM keyfile.
- `normalize(qcow2)`: upload fstab (lone `/`), rm crypttab; no SELinux/relabel (AppArmor needs none).
- Register `"debian": DebianFamily()` in `_FAMILIES`.

**Acceptance (test_debian.py, behavior not string-exactness):** debug argv enables `ssh.service` +
`kdump-tools.service`, sets `USE_KDUMP=1`, writes the NMI sysctl, injects the key, enables the
kdive-ready unit, stages `/usr/local/sbin/kdive-drgn`; cloud-image argv writes
`/etc/cloud/cloud-init.disabled` + seeds machine-id; argv never edits `/etc/selinux/config` nor stages
an NM keyfile; build argv omits kdump/NMI/helper; `normalize` writes fstab + removes crypttab and never
touches selinux.

## Task 3 — Family-aware provenance (`guest_mac`)

**Files:** `src/kdive/providers/local_libvirt/rootfs_build.py`,
`tests/providers/local_libvirt/test_rootfs_build.py`.

**Changes:** drop `_GUEST_SELINUX`; provenance records `"guest_mac": family.guest_mac` (resolved family
already in `build()`). Update the provenance assertion in the test (`guest_selinux` → `guest_mac:
"selinux-permissive"`). Add a test that the debian family yields `guest_mac: "apparmor"`.

**Acceptance:** provenance dict carries `guest_mac`; rhel build → `selinux-permissive`.

## Task 4 — Catalog entries + loader guard

**Files:** `fixtures/local-libvirt/rootfs_catalog.toml`, `tests/images/test_rootfs_catalog.py`.

**Changes:** append `debian-kdive-ready-12` (bookworm, sha256
`59f936c9…d330c`) and `debian-kdive-ready-13` (trixie, sha256 `e32d03ec…6416`), both
`family="debian"`, `kind="debug"`, `kdump_capable=false`, versioned serial URLs. Add both to
`_MAKEDUMPFILE_BY_NAME` (1.7.2 / 1.7.6); rename `test_rhel_entries_are_sha256_pinned_cloud_images` to
cover all cloud-image rows (or add a debian-specific load test).

**Acceptance:** `load_rootfs_catalog()` resolves both; the kdump_capable guard passes for the new rows;
both parse as `CloudImageSource` with 64-hex sha256.

## Task 5 — Docs + inventory

**Files:** `docs/operating/runbooks/image-lifecycle.md` (image table: 2 debian rows),
`systems.toml.example` (register debian-12/13 staged-path like rocky/centos).

**Acceptance:** runbook table lists both debian rows (makedumpfile 1.7.2/1.7.6, no, incomplete-core →
host_dump); systems.toml.example carries both `[[image]]` blocks. `just docs-*` guards green.

## Task 6 — Live-prove on the KVM host (operator step)

Build both via `build-fs --image debian-kdive-ready-{12,13}`, guestfish-inspect the result (apt set,
ssh/kdump-tools/kdive-ready enabled, no `/etc/selinux/config`, `/etc/cloud/cloud-init.disabled`),
direct-kernel-boot one on the v7.0.0 kernel to confirm the `kdive-ready` serial signal fires. Record
results in the spec's `### Naming, registration, and live-proof`. Not a CI gate; the env-gated
`live_vm` path. Failure here loops back to Task 2.

## Rollback

Each task is one or more small commits on `feat/debian-rootfs-catalog-824`; revert the offending commit.
No migrations, no schema, no persisted-contract change (provenance is a non-contract record).
