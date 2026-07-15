# ppc64le catalog parity across image families (#1152)

Date: 2026-07-14
Status: approved (design)
Epic: #1139 (full ppc64le support in the local-libvirt provider), sub-issue #13.
Depends on: #1147 (ADR-0345, cross-arch customization boot) — merged.
ADR: [0350](../adr/0350-ppc64le-catalog-parity.md)

## Goal

The rootfs catalog (`fixtures/local-libvirt/rootfs_catalog.toml`, ADR-0251) has one ppc64le row
(`fedora-kdive-ready-44-ppc64le`) against ~11 x86_64 rows. This issue brings the catalog to
ppc64le parity: every x86_64 **rhel-family** row whose distro publishes a ppc64le GenericCloud
qcow2 gains a sha256-pinned ppc64le sibling, and every row whose distro has no ppc64le cloud image
(or whose family cannot yet be built cross-arch) is documented N/A in the catalog rather than
silently skipped. At least one non-Fedora ppc64le row is proven end-to-end via a TCG customization
boot on the x86_64 host.

## Empirical availability (probed 2026-07-14)

| x86_64 row | ppc64le GenericCloud qcow2 published? | decision |
|---|---|---|
| `fedora-kdive-ready-43` (virt-builder) | n/a — virt-builder has no ppc64le templates | its `-cloud` sibling carries Fedora 43 parity |
| `fedora-kdive-ready-43-cloud` | `Fedora-Cloud-Base-Generic-43-1.6.ppc64le.qcow2` ✓ | add sibling |
| `fedora-kdive-ready-44` | already `fedora-kdive-ready-44-ppc64le` | exists |
| `fedora-kdive-build-44` (build host) | Fedora publishes ppc64le, but the ppc64le kernel-**build** lane is unproven in this epic | scope-note, not added (see Decision 5) |
| `rocky-kdive-ready-8` | `images/ppc64le/` is empty — Rocky 8 has no ppc64le port | **N/A** |
| `rocky-kdive-ready-9` | `Rocky-9-GenericCloud-Base-9.8-20260525.0.ppc64le.qcow2` ✓ | add sibling |
| `rocky-kdive-ready-10` | `Rocky-10-GenericCloud-Base-10.2-20260525.0.ppc64le.qcow2` ✓ | add sibling |
| `centos-stream-kdive-ready-9` | `CentOS-Stream-GenericCloud-9-20260622.0.ppc64le.qcow2` ✓ | add sibling |
| `centos-stream-kdive-ready-10` | `CentOS-Stream-GenericCloud-10-20260622.0.ppc64le.qcow2` ✓ | add sibling |
| `debian-kdive-ready-12` / `-13` | only `generic`/`nocloud` ppc64el (not the pinned `genericcloud`); debian family cannot cross-arch customize-boot until #1167 | **N/A (deferred to #1167)** |

Serials of the Rocky/CentOS ppc64le images match the x86_64 sibling exactly (same build serial,
different arch subtree); Fedora ppc64le lives in the `fedora-secondary` tree.

## Decisions

1. **Scope is the rhel family only.** All five new rows (Fedora 43-cloud, Rocky 9, Rocky 10,
   CentOS Stream 9, CentOS Stream 10) are `family = "rhel"`, which already has
   `customize_via = "boot"` (ADR-0345). No family-customizer code change is required to *add*
   these rows — they ride the existing arch-agnostic boot path. The issue's "family customizer
   quirks … dual-render form" scope item was anticipating the debian family, which #1167 owns.

2. **Version fields mirror the x86_64 sibling.** `makedumpfile_version` and `drgn_version` are the
   distro-repo package versions, which are arch-invariant within a release (the distro builds all
   arches from one source package). Each ppc64le sibling therefore carries the **same** two version
   values as its x86_64 sibling — the identical principle the existing `fedora-kdive-ready-44-ppc64le`
   row already documents ("Fedora 44 ships the same makedumpfile/drgn across arches"). This is a
   snapshot, not live upstream truth, exactly as for the x86_64 rows.

3. **N/A gaps are documented in the catalog, and encoded as a test.** Rocky 8 (no ppc64le port)
   and Debian 12/13 (no `genericcloud` ppc64el + family blocked on #1167) get an explicit N/A
   comment in `rootfs_catalog.toml`. A test asserts the catalog contains **no** ppc64le row for the
   debian family or for Rocky 8, so a future naive addition of a broken row fails loudly rather than
   shipping an un-buildable base.

4. **Debian ppc64le is deferred to #1167, not added.** Adding a debian ppc64le row now would ship a
   base that (a) uses a different image variant than its x86_64 sibling (`generic` vs
   `genericcloud`) and (b) cannot be customize-booted on the x86_64 host until the debian→boot
   migration (#1167, open) lands. Per "no speculative features / at minimum build-validated," a row
   that cannot be built on the only available host is not shipped. The catalog N/A comment points at
   #1167 so the follow-up is discoverable.

5. **The build-host row gets no ppc64le sibling.** `fedora-kdive-build-44` is a kernel-build
   toolchain image; a ppc64le build host is only useful to compile ppc64le kernels, and the ppc64le
   **build** lane is not proven anywhere in this epic. Shipping a ppc64le build row would be a
   speculative, unusable row. Documented as a scope note (not a distro-port N/A) pointing at the
   build-lane follow-up.

6. **Live proof: CentOS Stream 9 ppc64le, end-to-end under TCG.** The acceptance criterion "at least
   one non-Fedora family proven end-to-end via TCG customization boot" is met by customize-booting
   `centos-stream-kdive-ready-9-ppc64le` on the x86_64 host (the host has `qemu-system-ppc64` for
   TCG, proven by #1144/#1146). The remaining new rows are build-validated (catalog + loader tests).

## Live-proof risk: EL9 EPEL for drgn

`RhelFamily.customize_steps` runs `dnf -y install epel-release` **only** for `_el_major == 8`
(`rhel.py:108`), but the EL9 debug package set (`_EL8_EL9_DEBUG_PACKAGES`, applied for
`major <= 9`) includes `drgn`, which ships in **EPEL 9**, not EL9 BaseOS/AppStream. On Rocky 9,
`epel-release` sits in the default-enabled `extras` repo so a bare `dnf install drgn` *may* resolve;
on CentOS Stream 9 EPEL requires enabling CRB + installing `epel-release` first. This gap is
**pre-existing** (it affects the x86_64 EL9 rows too) and latent because no EL9 rhel row has been
customize-booted before. The CentOS Stream 9 live proof is the first exercise of that path.

Mitigation, decided in advance so the proof is not blocked:

- If the CS9 customize boot fails installing `drgn` for want of EPEL, fix it by making the rhel
  customizer enable EPEL for **every** EL major that installs `drgn` from EPEL (EL8 **and** EL9),
  not only EL8. This is an arch-agnostic correctness fix that also repairs the x86_64 EL9 rows; it
  is a legitimate "family customizer quirk surfaced by the proof" and stays in scope.
- The fix is narrow (widen the `== 8` guard to `<= 9` and, for CentOS Stream, enable CRB before
  `epel-release`). It does not alter the boot mechanism or any x86_64 byte contract beyond adding a
  transaction the EL9 rows already needed.

## Non-goals

- Debian ppc64le rows and the debian→boot migration (#1167).
- A ppc64le kernel-build lane / ppc64le build-host image.
- kdump crashkernel sizing, gdb/drgn, fadump on ppc64le (their own sub-issues, several merged).
- Any change to the catalog **loader** schema (`catalog.py`) — the existing `arch`/`source`/
  version fields already carry everything a ppc64le row needs; this issue is data + tests + one
  proof, not a schema change.

## Acceptance criteria

- Five sha256-pinned ppc64le rhel-family rows added, versions mirroring their x86_64 siblings.
- `tests/images/test_rootfs_catalog.py` covers every new row: `arch == "ppc64le"`, cloud-image
  source with a 64-char sha256 and the arch token in the URL, `family == "rhel"`, and the
  version-parity invariant against the x86_64 sibling.
- A test encodes the N/A decision (no debian/Rocky-8 ppc64le row).
- N/A gaps enumerated in `rootfs_catalog.toml` comments (Rocky 8, Debian, and the build-host
  scope note).
- CentOS Stream 9 ppc64le customize-boots end-to-end under TCG on the x86_64 host, recorded in a
  proof-record doc; remaining rows build-validated. `just ci` green.
