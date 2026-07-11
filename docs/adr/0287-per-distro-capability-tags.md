# ADR 0287 — Per-distro static capability tags via a family-declared `capabilities()` seam

- **Status:** Accepted
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
table is wrong in three directions at once:

- it **asserts a false tag**: `agent` is the QEMU guest agent
  (`GUEST_CONTRACT_PATHS["agent"] = "/usr/sbin/qemu-ga"`, `images/validation.py`), the remote
  provider's access seam — but the local families install **no** qemu-guest-agent;
  local-libvirt drives guests over SSH plus the `kdive-ready` serial unit. `agent` is false
  for every local staged image;
- it **omits** traits every debug image does bake: `openssh-server` is explicitly installed,
  and a mandatory-access-control posture is set; and
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

See `docs/archive/superpowers/specs/2026-07-01-rootfs-capability-tags-s1-design.md`.

## Decision

Move capability-tag authorship from the fixed `_KIND_CAPABILITIES` table into the family,
next to the package logic that already owns per-distro divergence, and keep it honest with a
structural guard.

- **New vocabulary.** Add `ssh`, `selinux`, and `apparmor` to the closed `Capability`
  StrEnum (now `{agent, kdump, drgn, build, helpers, ssh, selinux, apparmor}`). The
  ADR-0286 vocabulary guard (`GUEST_CONTRACT_PATHS` keys ⊆ `Capability`) is unaffected —
  the new members only widen the enum; `helpers`/`drgn` guest-contract semantics are
  untouched.

- **Drop the false `agent` tag from local images.** The local families do not install
  qemu-guest-agent, so they do not declare `agent`. The `agent` member stays in the enum,
  reserved for the remote provider's guest contract (`GUEST_CONTRACT_PATHS`). The honest
  "you can drive this guest" tag for a local image is `ssh`.

- **Family-declared capabilities.** Add a `capabilities(kind, distro, version) ->
  tuple[Capability, ...]` method to the `FamilyCustomizer` protocol, mirroring `packages()`.
  Each family returns exactly the tags its `packages()`/`customize_argv` install. `ssh` is
  declared only where `openssh-server` is explicitly in `packages()` (the debug sets), so the
  tag never rests on the base image happening to ship sshd; the MAC posture is on both kinds;
  `kdump`/`drgn` are debug-only; `build` marks the build kind:

  | family · kind | capabilities |
  | --- | --- |
  | rhel · debug | `ssh, selinux, kdump, drgn` |
  | rhel · build | `selinux, build` |
  | debian · debug | `ssh, apparmor, kdump, drgn` |
  | debian · build | `apparmor, build` |

  `catalog_rootfs_build` calls `family.capabilities(...)` instead of indexing
  `_KIND_CAPABILITIES`; the table is **deleted** (no shim). Tags are EL-major-invariant:
  EL 8/9 and Fedora differ in *packages* (drgn via EPEL, makedumpfile bundled in
  `kexec-tools`, openssh-server in each debug set), not in the resulting trait set.

- **Anti-drift guard.** A test iterates the live family registry (`_FAMILIES`) × both kinds
  and asserts every declared tag is backed by concrete build evidence: `ssh` ⇒ `openssh-server`
  in `packages()`; `selinux`/`apparmor` ⇒ agreement with `family.guest_mac`; `kdump` ⇒ a
  kdump package in `packages()`; `drgn` ⇒ a drgn package; `build` ⇒ the build kind. Iterating
  the registry (not a hand-listed family set) means a newly-registered family is covered
  automatically, including that its `guest_mac` maps to a MAC tag. The guard enforces
  **declaration ↔ recipe** consistency — that a family declares exactly what its own
  `packages()`/`customize_argv` install. It does **not** prove **recipe ↔ efficacy** (that a
  `systemctl enable` was not a no-op, that sshd answers); that runtime truth is the S2
  boot-probe's job. S1 tags mean "the build installs this", and the guard keeps that claim
  from drifting from the recipe.

- **Converge the staged-path metadata.** Update the hand-authored staged-path
  `capabilities` in the repo (`fixtures/local-libvirt/`, `systems.toml.example`,
  `admin/default_fixtures.py`, the operating-docs example) to the family-accurate per-distro
  sets, so a declared local image matches what `build-fs` would stamp. Operator-owned
  `~/.config/kdive/systems.toml` is corrected out-of-band.

## Consequences

- `images.describe` now differentiates rhel (`selinux`) from debian (`apparmor`), shows `ssh`
  as a baked trait, and no longer advertises a phantom `agent` on local images; the metadata
  stops lying by assertion, by omission, and by flattening.
- Dropping `agent` from local images is a visible change to every local staged image's
  `capabilities`. No local code path gates on `agent` (it is a remote guest-contract element;
  local resolution/boot does not consult it), so nothing regresses; readers that displayed the
  tag will simply stop seeing a value that was never true.
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

- **Keep declaring `agent` on local images (install qemu-guest-agent, or redefine `agent`).**
  Installing qemu-ga would make the tag true but adds a package local provisioning never uses
  (local drives guests over SSH) and changes build behavior S1 is meant to keep constant.
  Redefining `agent` to mean "kdive-ready readiness" would reinterpret an existing
  remote-provider contract (`GUEST_CONTRACT_PATHS`, the upload path), a far larger blast
  radius than the phantom tag warrants. Rejected both; drop the tag locally instead.

- **Promote MAC posture as a single `mac` tag carrying the mode.** A flat `text[]` tag
  cannot carry `permissive`/`enforcing` structure cleanly; `selinux` vs `apparmor` as
  distinct members expresses the one fact that differs per distro, and the
  permissive/enforcing detail stays in `provenance["guest_mac"]`. Rejected the encoded-tag
  form.
