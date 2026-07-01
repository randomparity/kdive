# ADR 0287 — Per-distro static capability tags via a family-declared `capabilities()` seam

- **Status:** Proposed
- **Date:** 2026-07-01
- **Deciders:** kdive maintainers

## Context

ADR-0286 (#957) made `capabilities` a validated build fact and reframed liveness as a
separate computed signal, but it left the build side coarse: `images/rootfs_specs.py`
derives an image's tags from a single fixed table,

```python
_KIND_CAPABILITIES = {
    "debug": (Capability.AGENT, Capability.KDUMP, Capability.DRGN),
    "build": (Capability.AGENT, Capability.BUILD),
}
```

so every debug image — Fedora, Rocky, CentOS Stream, Debian — is stamped with the identical
`(agent, kdump, drgn)` tuple regardless of what its `FamilyCustomizer` actually bakes. That
table is wrong in two directions at once:

- it **omits** traits every family does bake (`customize_argv` enables sshd, injects the
  managed key, uploads the readiness unit, and sets a mandatory-access-control posture for
  **both** kinds); and
- it **flattens** a genuine per-distro difference: the rhel family sets SELinux
  (`guest_mac = "selinux-permissive"`), the debian family uses AppArmor
  (`guest_mac = "apparmor"`). Two images with materially different MAC postures describe
  themselves identically.

The distro-specific knowledge already lives in the family customizers (`RhelFamily`,
`DebianFamily`), which vary `packages()` per distro and EL-major. Capabilities are computed
in `rootfs_specs.py` from a constant, so that knowledge cannot reach the tag set.

This is the first slice (S1) of a four-part effort to make rootfs capabilities honest and
per-distro. S2 adds a boot-probe harness in `build-fs` (reusing the local-libvirt lifecycle)
that verifies runtime truths and records operands; S3 (`sysrq`) and S4
(`direct_kernel_bootable`) add computed signals on that harness. S1 is boot-free and purely
descriptive: it makes the *tags* tell the per-distro truth of what today's build already
bakes. It adds no new build behavior.

See `docs/superpowers/specs/2026-07-01-rootfs-capability-tags-s1-design.md`.

## Decision

Move capability-tag authorship from the fixed `_KIND_CAPABILITIES` table into the family,
next to the package logic that already owns per-distro divergence, and keep it honest with a
structural guard.

- **New vocabulary.** Add `ssh`, `selinux`, and `apparmor` to the closed `Capability`
  StrEnum (now `{agent, kdump, drgn, build, helpers, ssh, selinux, apparmor}`). The
  ADR-0286 vocabulary guard (`GUEST_CONTRACT_PATHS` keys ⊆ `Capability`) is unaffected —
  the new members only widen the enum; `helpers`/`drgn` guest-contract semantics are
  untouched.

- **Family-declared capabilities.** Add a `capabilities(kind, distro, version) ->
  tuple[Capability, ...]` method to the `FamilyCustomizer` protocol, mirroring `packages()`.
  Each family returns exactly the tags it bakes:

  | family · kind | capabilities |
  | --- | --- |
  | rhel · debug | `agent, ssh, selinux, kdump, drgn` |
  | rhel · build | `agent, ssh, selinux, build` |
  | debian · debug | `agent, ssh, apparmor, kdump, drgn` |
  | debian · build | `agent, ssh, apparmor, build` |

  `catalog_rootfs_build` calls `family.capabilities(...)` instead of indexing
  `_KIND_CAPABILITIES`; the table is **deleted** (no shim). Tags are EL-major-invariant:
  EL 8/9 and Fedora differ in *packages* (drgn via EPEL, makedumpfile bundled in
  `kexec-tools`), not in the resulting trait set.

- **Anti-drift guard.** A test asserts every declared tag is backed by concrete build
  evidence for each family × kind: `ssh` ⇒ an ssh-enable/`--ssh-inject` step in
  `customize_argv`; `selinux`/`apparmor` ⇒ agreement with `family.guest_mac`; `kdump` ⇒ a
  kdump package in `packages()`; `drgn` ⇒ a drgn package; `agent` ⇒ the readiness unit is
  uploaded; `build` ⇒ the build kind. If baking and declaration diverge later, the guard
  fails. This is the structural mechanism that keeps the tags from decaying back into the
  fiction ADR-0286 set out to eliminate.

- **Converge the staged-path metadata.** Update the hand-authored staged-path
  `capabilities` in the repo (`fixtures/local-libvirt/`, `systems.toml.example`,
  `admin/default_fixtures.py`, the operating-docs example) to the family-accurate per-distro
  sets, so a declared local image matches what `build-fs` would stamp. Operator-owned
  `~/.config/kdive/systems.toml` is corrected out-of-band.

## Consequences

- `images.describe` now differentiates rhel (`selinux`) from debian (`apparmor`) and shows
  `ssh` as a baked trait; the metadata stops lying by omission and by flattening.
- Per-distro tag knowledge has one home (the family), reachable by both the build argv and
  the declared tag set, with a guard tying them together.
- `_KIND_CAPABILITIES` is removed; callers and tests that referenced it or the old 3-tuple
  are updated in the same change.
- No migration (tags are `text[]`, ADR-0286), no boot, no new build behavior. `build-fs`
  output bytes are unchanged; only recorded/declared capabilities change.
- The enum grows by three members; downstream readers that switch on capabilities must
  tolerate the new tags (all current readers treat `capabilities` as an opaque set, so none
  regress).

## Considered & rejected

- **Derive tags in `rootfs_specs.py` from `family.packages()` membership + `guest_mac`.**
  No new family method, but it re-implements distro-specific derivation (openssh-server →
  `ssh`, `guest_mac` → `selinux`/`apparmor`) *outside* the family that owns the knowledge,
  reintroducing the split ADR-0286 fought. Rejected for the leak.

- **Hand-declare capabilities per catalog entry in `rootfs_catalog.toml`.** Simplest, but
  it is exactly the hand-maintained metadata that drifts; the build and the declaration
  would diverge with nothing to catch it. Rejected.

- **Bake a `kernel.sysrq` sysctl and add a `sysrq` tag in S1.** `sysrq` availability is
  entangled with the kernel's `CONFIG_MAGIC_SYSRQ` (a build-config fact) and is only
  honestly confirmable by the S3 boot-probe. Adding a static `sysrq` tag in S1 would assert
  a trait the rootfs alone does not guarantee. Deferred to S3, where it is verified
  end-to-end.

- **Promote MAC posture as a single `mac` tag carrying the mode.** A flat `text[]` tag
  cannot carry `permissive`/`enforcing` structure cleanly; `selinux` vs `apparmor` as
  distinct members expresses the one fact that differs per distro, and the
  permissive/enforcing detail stays in `provenance["guest_mac"]`. Rejected the encoded-tag
  form.
