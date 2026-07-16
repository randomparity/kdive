# Platform and architecture support

KDIVE runs the local-libvirt kernel-debug loop on **x86_64** and on **ppc64le**
(POWER9/POWER10). The two arches are not at the same tier: x86_64 with hardware
KVM is the primary, fully validated target; ppc64le is supported natively on
POWER and available — slowly — under cross-arch TCG emulation on an x86_64 host.

This page states what is actually supported and to what tier, and flags the two
places where the matrix is *not* full parity: the EL9 customize-boot proof on
ppc64le, and fadump on native POWER. It describes shipped reality — where a proof
has been recorded it is cited; where a path rides a shared mechanism without its
own end-to-end proof, it says so.

For the development-side arch differences (the ppc64le Rust toolchain requirement,
the container images with no ppc64le manifest, the multi-arch publish), see the
[cross-platform development guide](../development/cross-platform.md). For a
from-scratch POWER box, see the
[POWER host bring-up runbook](runbooks/power-host-bringup.md).

## Architecture and accelerator tiers

| Arch + accelerator | Tier | Notes |
|---|---|---|
| **x86_64 + KVM** | Primary — fully supported | Hardware virtualization (`/dev/kvm`). The default local-libvirt target; every spine step is proven here. |
| **ppc64le + KVM-HV** (POWER9/POWER10) | Supported | Native KVM-HV on a real POWER host. Validated end-to-end for the kdump spine on POWER9/POWER10. |
| **ppc64le + TCG** (on an x86_64 host) | CI-only / slow | Software-emulated foreign-arch guest. Boots and runs, but an order of magnitude slower than KVM; the boot-deadline multiplier applies (see below). Used to prove the ppc64le paths in CI without POWER hardware. |
| x86_64 + TCG | not a target | No reason to emulate the native arch; use KVM. |

### The TCG boot-deadline multiplier

TCG (software emulation) executes a foreign-arch guest roughly an order of
magnitude slower than hardware KVM, so a boot that is healthy on KVM would trip a
KVM-tuned deadline under TCG. The provider scales every guest-execution deadline by
a single multiplier keyed off the System's persisted accelerator: `1.0` for KVM,
and a configurable factor (`KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER`, **default
`10.0`**) for TCG — and, deliberately, for an unknown/`NULL` accelerator, so an
un-classified guest is never starved of boot time. This is why ppc64le-under-TCG
runs are slow but do not spuriously fail readiness.

## Distro customize-boot matrix

"Customize-boot" is how KDIVE bakes a debug-ready base image: it boots the vendor
cloud image once, runs the first-boot customization (install `drgn`, `kdump`,
`openssh-server`, seal), and publishes the sealed image. The rhel family
(Fedora, Rocky, CentOS Stream) customizes via an in-guest **boot** pass, which is
cross-arch capable; the debian family still customizes via `virt_customize`, which
is x86_64-only today.

The column that matters for a newcomer is whether a given distro has actually
reached the `kdive-customize-ok` marker and published on a given arch.

Legend: ✅ verified end-to-end (proof recorded) · ◐ supported via the shared
family mechanism (no dedicated end-to-end proof) · ⚠️ known gap / gated · — not
available.

| Distro | Family | x86_64 (KVM) | ppc64le (KVM on POWER / TCG) |
|---|---|---|---|
| Fedora 43, 44 | rhel (boot) | ✅ verified | ✅ verified (live under TCG) |
| Rocky 9 / CentOS Stream 9 (EL9) | rhel (boot) | ✅ verified (Rocky 9) | ⚠️ **not verified — gated** (see below) |
| Rocky 10 / CentOS Stream 10 (EL10) | rhel (boot) | ◐ shared EL boot path | ⚠️ **not verified — gated** (same as EL9) |
| Rocky 8 (EL8) | rhel (boot) | ◐ shared EL boot path | — no ppc64le port (Rocky 8 is x86_64 + aarch64) |
| Debian 12, 13 | debian (`virt_customize`) | ✅ supported | — deferred (#1167) |

Notes on the matrix:

- **Fedora** is the reference distro on both arches. The x86_64 path is the
  baseline; the ppc64le path is proven live — a `fedora-kdive-ready-44-ppc64le`
  image builds, reaches `kdive-customize-ok`, and publishes under TCG.
- **EL9 x86_64** is proven end-to-end: a `rocky-kdive-ready-9` customize boot
  reaches `kdive-customize-ok` and publishes on x86_64 KVM
  ([#1174 proof record](../design/2026-07-15-el9-customize-boot-1174-proof-record.md)).
  CentOS Stream 9 rides the identical rhel-family boot path.
- **EL10 and Rocky 8 on x86_64** ship catalog- and loader-validated and ride the
  same rhel-family boot mechanism as the proven EL9 path, but do not have their own
  recorded end-to-end customize-boot proof — hence ◐, not ✅.
- **Debian ppc64le** is deferred to **#1167**: Debian publishes only the
  `generic`/`nocloud` ppc64el variant (not the `genericcloud` variant the x86_64
  rows pin), and the debian family still uses `virt_customize`, which cannot
  cross-arch customize-boot on an x86_64 host.

### Known gap — EL9 customize-boot on ppc64le (#1174)

An EL9 (Rocky 9 / CentOS Stream 9) customize boot has **not** reached
`kdive-customize-ok` on ppc64le. The x86_64 proof above satisfies the composed-path
acceptance ("on at least one arch"), but the ppc64le arch proof is gated on an
**environmental** blocker, not kdive code: under the TCG/SLIRP emulated network the
CentOS `dnf4` metadata download stalls at 0 B/s against the mirror CDN, so first-boot
`dnf` exhausts its mirrors and exits before the install completes (Fedora's `dnf5`
and CDN are reliable under the same SLIRP, which is why Fedora ppc64le passes). It is
solvable only on native POWER (KVM-HV, real network), which is the separate
live-hardware track. Until that proof lands, treat EL9 customize-boot on ppc64le as
**unverified**, and use Fedora ppc64le for a customize-boot-verified ppc64le image.
See the [#1174 proof record](../design/2026-07-15-el9-customize-boot-1174-proof-record.md).

## Crash-capture methods by arch

kdump is the supported crash-capture spine on both arches. fadump is a ppc64le-only,
opt-in method with additional host and profile requirements.

| Method | x86_64 | ppc64le | Notes |
|---|---|---|---|
| kdump | ✅ supported | ✅ supported | The default spine; validated natively on POWER (POWER9/POWER10) and under TCG. |
| fadump | — (POWER-only mechanism) | ⚠️ opt-in, with limitations | See below. |
| host_dump | ✅ supported | ✅ supported | QEMU-side dump; arch-agnostic. |

### Known limitation — fadump on native POWER (#1181)

fadump (firmware-assisted dump) is **opt-in** and carries hard requirements:

- **QEMU ≥ 10.2** on the host — the pseries fadump RTAS floor. Under TCG the fadump
  proof *skips* (RTAS unsupported), so fadump is a **native-POWER-only** path in
  practice.
- **Native KVM-HV** on a real POWER host.
- **A 4 GiB guest-RAM floor**, enforced at admission. fadump reserves a boot-memory
  region *on top of* `crashkernel`; at the default 2 GiB profile the reservation
  leaves too little RAM for the guest to reach readiness, so a fadump profile below
  4096 MiB is rejected at `systems.provision` / `systems.define` with a
  `CONFIGURATION_ERROR` (before any capacity commit).

The RAM-floor fix has landed, and a fadump-ready POWER10 host boots `fadump=on` under
KVM-HV, but the **end-to-end native fadump crash→capture at the 4 GiB floor is not
yet proven** — it awaits a fully-provisioned POWER live-stack. Until then, use
**kdump** (the fully validated spine) on ppc64le. See the
[#1181 proof record](../design/2026-07-15-power-native-fadump-ram-floor-1181-proof-record.md).

## Summary

- **x86_64 + KVM** is the primary, fully supported target — use it unless you
  specifically need POWER.
- **ppc64le + KVM-HV on POWER** is supported for the kdump spine; **ppc64le + TCG**
  works for CI and cross-arch checks but is slow (10× boot-deadline multiplier).
- For a customize-boot-verified ppc64le image, use **Fedora**; EL9 customize-boot on
  ppc64le (#1174) and native-POWER fadump capture (#1181) are the two known gaps and
  are flagged above rather than implied to be at parity.
