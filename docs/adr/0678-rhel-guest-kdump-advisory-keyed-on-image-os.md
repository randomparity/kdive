# 0678 — Check the RHEL-family kdump set at `runs.complete_build`, keyed on the target image's OS

## Status

Accepted (2026-09-25)

Amends [ADR-0478](0478-rhel-guest-kdump-config-requirements.md) §1 and its Consequences, and
restates [ADR-0398](0398-complete-build-effective-config-upload-nudge.md)'s advisory exclusivity
for a third advisory.

## Context

ADR-0478 moved the RHEL-family kdump symbols (`XFS_FS`, `SQUASHFS`, `SQUASHFS_ZSTD`, `EROFS_FS`,
`OVERLAY_FS`, `BLK_DEV_LOOP`, `KEXEC_FILE`) into the advertise-only `crash_capture_rhel_guest`
feature and left it unchecked, because kdive had no runtime signal for the guest's OS family. It
named the missing axis: the image catalog. That signal now exists. A local-libvirt catalog rootfs
resolves to one registered public `image_catalog` row (the lane `vmcore.fetch`'s kdump capability
gate already resolves, ADR-0361), and a build records `provenance.os_release.id` on that row
(ADR-0311). #2762 is the third live loop (#688, #1626) spent finding this set one symbol at a
time, after the crash, on a Fedora guest.

## Decision

1. `runs.complete_build` reads the uploaded `effective_config` against `crash_capture_rhel_guest`'s
   advertised clauses and, when any is unmet, adds `data.rhel_guest_crash_config`
   (`{reason: "kernel_missing_rhel_guest_crash_config", missing, remediation, guest_family}`) and
   the contract ref. The completion still succeeds. The entry's `enforcement` becomes
   `upload_advisory`.
2. The guest family comes from the target image's `os_release.id`: `fedora`, `rhel`, `centos`,
   `rocky`, `almalinux` are RHEL-family (`guest_family: "rhel"`); any other recorded id is silent.
   When kdive cannot resolve an id (Run not yet bound to a System, a rootfs that is not a local
   catalog image, no registered row, no recorded `os_release`), it warns with
   `guest_family: "unknown"` and says the check is conditional on the guest being RHEL-family.
3. Advisory exclusivity (ADR-0398) is restated: `missing_effective_config` still excludes both
   warnings, because both need a present config. `missing_boot_config` and
   `rhel_guest_crash_config` are independent and may appear together.
4. A kdump-family capture that finds no core adds a static `kernel_config_hint` to the
   `readiness_failure` details, naming `crash_capture_rhel_guest` and the contract, in the local
   provider and in the remote provider's kdump capture. `host_dump` does not carry it.

## Consequences

- A Fedora/RHEL-family catalog System now learns the missing symbols before install, and a
  decoupled Run (#1881) gets the conditional `unknown` warning rather than silence.
- An `unknown` warning reaches some non-RHEL guests (e.g. an unbound Run later bound to Debian).
  It says so in its remediation. This is narrower than ADR-0478's rejected every-upload advisory:
  a known non-RHEL image and a config carrying the whole set stay silent.
- Remote-libvirt Systems resolve to `unknown`; the capture hint is their family-independent
  signal.
- Nothing refuses. ADR-0478 §2 and §3 stand unchanged.

## Considered & rejected

- **Read the build-fs catalog's `family` by image name.** verified:
  `src/kdive/images/rootfs/catalog.py:18` sets `DEFAULT_CATALOG_PATH` to
  `Path(__file__).parents[4] / "fixtures"`, outside the installed package, so a server running
  from a wheel has no file to read; operator-declared images have no row there either.
- **Stay silent when the family is unknown.** judgment: the decoupled path has no System at
  `complete_build`, so the case #2762 reports would stay silent for every such Run.
- **Warn on every upload regardless of family.** judgment: ADR-0478 rejected this; a known
  non-RHEL image is the common case and gains nothing from it.
- **Repeat the check at `runs.install` or `control.force_crash`.** judgment: a second pre-crash
  seam for one advisory; the `unknown` warning and the capture hint cover the decoupled path.
- **Resolve the Run's config and family inside the retrieve providers.** judgment: providers
  hold no database connection; a static pointer is what an empty capture can honestly say.
