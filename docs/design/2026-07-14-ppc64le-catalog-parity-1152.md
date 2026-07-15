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

1. **Scope is the rhel family only.** All five new rows are `family = "rhel"`, which already has
   `customize_via = "boot"` (ADR-0345). No family-customizer code change is required to *add*
   these rows — they ride the existing arch-agnostic boot path. The issue's "family customizer
   quirks … dual-render form" scope item was anticipating the debian family, which #1167 owns.
   The naming rule is `<x86_64-row-name>-ppc64le`, matching the existing
   `fedora-kdive-ready-44-ppc64le`. The exact five new row names are therefore:

   | new ppc64le row | x86_64 sibling (version-parity source) | pinned image |
   |---|---|---|
   | `fedora-kdive-ready-43-cloud-ppc64le` | `fedora-kdive-ready-43-cloud` | `Fedora-Cloud-Base-Generic-43-1.6.ppc64le.qcow2` |
   | `rocky-kdive-ready-9-ppc64le` | `rocky-kdive-ready-9` | `Rocky-9-GenericCloud-Base-9.8-20260525.0.ppc64le.qcow2` |
   | `rocky-kdive-ready-10-ppc64le` | `rocky-kdive-ready-10` | `Rocky-10-GenericCloud-Base-10.2-20260525.0.ppc64le.qcow2` |
   | `centos-stream-kdive-ready-9-ppc64le` | `centos-stream-kdive-ready-9` | `CentOS-Stream-GenericCloud-9-20260622.0.ppc64le.qcow2` |
   | `centos-stream-kdive-ready-10-ppc64le` | `centos-stream-kdive-ready-10` | `CentOS-Stream-GenericCloud-10-20260622.0.ppc64le.qcow2` |

   The version-parity test maps ppc64le→x86_64 sibling by stripping the `-ppc64le` suffix, so the
   naming rule is mechanical. (`fedora-kdive-ready-44-ppc64le` already follows it.)

2. **Version fields mirror the x86_64 sibling — with one caveat for EPEL drgn.**
   `makedumpfile_version` is a base-distro / `kexec-tools` package, arch-invariant within a release
   (the distro builds all arches from one source package), so mirroring the x86_64 value is safe.
   `drgn_version` is arch-invariant **for Fedora** (base repo), but on EL8/EL9 `drgn` comes from
   **EPEL**, a separate per-arch build system whose ppc64le build can in principle lag or differ.
   For the EL9 rows (Rocky 9, CentOS Stream 9) the mirrored `drgn_version` is therefore
   **verified against the ppc64le EPEL index** (one index probe, exactly as the x86_64 rows were),
   not merely copied; if the probe disagrees, the ppc64le row carries the ppc64le-index value and a
   comment. Either way the CS9 live proof records the actually-installed drgn in the image
   provenance, which reconciles against the row. This is a snapshot, not live upstream truth,
   exactly as for the x86_64 rows. The existing `fedora-kdive-ready-44-ppc64le` row (Fedora, base
   repo) already documents the arch-invariance ("Fedora 44 ships the same makedumpfile/drgn across
   arches").

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
   TCG, proven by #1144/#1146).

   **Falsifiable pass signal.** The proof PASSES iff the per-build `kdive-build-<uuid>` domain emits
   the `kdive-customize-ok` marker on `hvc0` (not a mere readiness/SSH heuristic — the marker is the
   authoritative completion signal per ADR-0345) and the sealed image is produced; it FAILS on the
   `kdive-customize-failed` marker or the TCG-scaled deadline, with the `redacted_console_tail`
   captured as evidence. Recorded in a proof-record doc alongside the other epic proofs.

   **"Build-validated" for the other four rows means catalog/loader validation only** — the loader
   test resolves the row, asserts the `cloud-image` source with a 64-char sha256 and the arch token
   in the URL, and checks version parity. It does **not** fetch the image, run `build-fs`, or boot.
   Full customize-boot is proven for exactly one row (CS9) because each TCG boot is slow; the plan
   additionally HEAD/resolve-checks each pinned URL so a 404 is caught without a full build. This is
   the honest reading of the issue's "at minimum build-validated"; it is weaker than "the image
   builds," and that is stated, not hidden.

## Live-proof outcome for risk 1: fsck feature-skew found and fixed here (ADR-0351)

The CentOS Stream 9 ppc64le customize boot **did** boot its kernel and dracut under TCG, then
failed at `systemd-fsck-root` on `/dev/vda` → emergency mode. Root cause (confirmed on the produced
image): the repack's ext4 carries `orphan_file`, an e2fsprogs-1.47 feature the Fedora-44 libguestfs
appliance stamps by default, which EL9's 1.46.5 e2fsck rejects. It is distro-userspace skew, not
arch (x86_64 EL9 would fail identically), latent because #1147 proved only Fedora. Per the issue
owner's decision, this is **fixed in this issue** (not deferred): the repack strips `orphan_file`
so any EL ≤ 9 guest can fsck the root — **[ADR-0351](../adr/0351-repack-ext4-older-guest-fsck-compat.md)**.
The fallback below is retained only as the contingency had the fix proven infeasible.

## Live-proof risk 1 (contingency): does a non-Fedora ppc64le image customize-boot at all?

Every prior ppc64le boot proof (#1144/#1146) used **Fedora**. The customization boot direct-kernel-
boots a baseline kernel extracted from the repacked image (ADR-0272/0345). Whether a **CentOS
Stream 9 ppc64le** GenericCloud image yields a single extractable baseline kernel and boots on
`pseries` under TCG through that machinery is the gating unknown for any non-Fedora ppc64le row — it
is logically prior to the EPEL/drgn question below (EPEL only matters once the image boots and runs
the firstboot script). Mitigation: the proof is a falsification gate. If CS9 does not boot under the
customization machinery, the finding is recorded (with console tail) and the proof falls back to the
next non-Fedora candidate (`rocky-kdive-ready-9-ppc64le`, or a Fedora ppc64le row if no non-Fedora
row boots) so the acceptance criterion cannot silently stall; the boot-blocking finding then becomes
its own follow-up. The five catalog rows and their tests ship regardless of which row carries the
live proof.

## Live-proof risk 2, fixed: EPEL for drgn on every EL clone (not just EL8)

`RhelFamily.customize_steps` ran `dnf -y install epel-release` **only** for `_el_major == 8`, but
the debug package set installs `drgn` on **every** EL major, and `drgn` ships in **EPEL** on all of
them — EPEL 8/9/**10** — never in EL BaseOS/AppStream (the catalog rows record `EPEL 9`/`EPEL 10`
sources). So EL9 **and EL10** rows could not install `drgn`; the gap was latent because no EL rhel
row had been customize-booted before (only Fedora, in #1147). Investigating the CS9 proof made this
concrete.

Fixed here (arch-agnostic; also repairs the latent x86_64 EL9/EL10 rows): the guard widens from
`_el_major(...) == 8` to `_el_major(...) is not None` — i.e. **every EL clone enables EPEL before
the drgn install; only Fedora (major `None`, base-repo drgn) does not**. `epel-release` sits in a
default-enabled extras repo on both Rocky (`extras`) and CentOS Stream (`extras-common`), so the
existing one-transaction `dnf -y install epel-release` needs no prior CRB enable to resolve
`epel-release` itself. If the live proof shows a `drgn` dependency that additionally requires CRB,
that is recorded from the proof output and added as a version-keyed step — not guessed here. The
change touches no boot mechanism and no x86_64 byte contract beyond the EPEL transaction the EL rows
always needed. Guarded by `test_el_clones_enable_epel_before_installing_drgn` /
`test_fedora_does_not_enable_epel`.

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
- Each pinned ppc64le URL resolves at build time (HEAD/checksum check documented in the plan), so a
  pruned-serial 404 is caught without a full build. CentOS Stream prunes old dated serials from
  `cloud.centos.org`, so the pinned serials (`20260622.0`) are the same durability class as the
  x86_64 rows — confirmed resolving now, expected to rot on the same policy.
- One non-Fedora ppc64le row (CS9, or the documented fallback) customize-boots end-to-end under TCG
  on the x86_64 host, passing on the `kdive-customize-ok` `hvc0` marker, recorded in a proof-record
  doc. The remaining four rows are **catalog/loader-validated** (row resolves, source/arch/sha256/
  version-parity asserted) plus URL-resolve-checked — not full-build-validated (stated honestly in
  Decision 6). `just ci` green.
