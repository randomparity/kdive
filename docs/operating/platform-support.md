# Platform and architecture support

KDIVE implements local-libvirt paths for `x86_64` and `ppc64le`. This page separates current
catalog and runtime behavior from recorded live evidence. A dated proof establishes that tested
configuration and revision; it does not certify every distro release or the current checkout.

Use the [cross-platform guide](../development/cross-platform.md) for development prerequisites,
container choices and native POWER host integration. Use [live testing](runbooks/live-testing.md)
to validate a deployment.

## Architecture and accelerator tiers

| Guest and accelerator | Implementation and evidence |
|---|---|
| x86_64 + KVM | Primary local-libvirt target; exercised by the native live suite. |
| ppc64le + KVM-HV | Native POWER path. The [July 2026 proof](../design/2026-07-15-power-native-kvm-hv-validation-1156-proof-record.md) records the kdump spine on POWER9. POWER10 shares the architecture; that record is not a POWER10 end-to-end result. |
| ppc64le + TCG | Foreign-architecture path on x86_64, used by the hosted live tier. Recorded [uploaded boot](../design/2026-07-13-ppc64le-boot-bundle-proof-record-1146.md) and [kdump](../design/2026-07-13-ppc64le-kdump-proof-record-1148.md) proofs. |
| x86_64 + TCG | Available for foreign x86_64 guests on POWER or native guests without KVM. Availability alone is not a dedicated end-to-end proof. |

### The TCG boot-deadline multiplier

The local provider scales two guest waiting windows: ordinary boot readiness and image
customization completion. KVM uses `1.0`; TCG and an unknown/`NULL` accelerator use
`KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER` (default `10.0`, minimum `1.0`). Set `1.0` to disable
scaling. This increases those windows; it does not scale every job, request or guest-operation
timeout, or guarantee readiness on a slow host. The [configuration reference](../guide/reference/config.md)
owns the base windows and setting contract.

### Cross-architecture guests

The local provider uses TCG for foreign-architecture guests. Native guests use KVM when it is
available and can fall back to TCG; inspect the worker's diagnostic result rather than assuming
hardware acceleration from the host architecture alone.

To enable foreign-arch guests, install the foreign arch's QEMU system emulator. The package
name is distro-specific (and matches what `scripts/check-setup-deps.sh` reports):

| distro | ppc64le emulator (`qemu-system-ppc64`) | x86_64 emulator (`qemu-system-x86_64`) |
|--------|----------------------------------------|----------------------------------------|
| Fedora / RHEL / CentOS | `qemu-system-ppc` | `qemu-system-x86` |
| Debian / Ubuntu | `qemu-system-ppc` | `qemu-system-x86` |
| Arch | `qemu-system-ppc` | `qemu-system-x86` |
| openSUSE | `qemu-ppc` | `qemu-x86` |

For example, to enable ppc64le guests on an x86_64 Fedora host: `dnf install qemu-system-ppc`.

For a local-libvirt worker, two diagnostics report the per-arch accelerator:

- `scripts/check-setup-deps.sh` prints a cross-arch line per foreign arch — "available via
  TCG only" when its emulator is present, or the exact package to install when it is not.
- The service `doctor` (`kdivectl doctor --json`) carries a `guest_arch_accel` check whose
  `data` maps each schedulable arch to `kvm` or `tcg`, and which fails only when the host
  lacks its own native-arch emulator. This is a worker-local probe; a remote provider does not
  require that worker to have the remote host's emulator.

The [deadline multiplier](#the-tcg-boot-deadline-multiplier) applies to emulated guests.

## Distro customize-boot matrix

`build-fs` customizes catalog images by booting them and running their family package manager.
The rhel family covers Fedora, Rocky and CentOS Stream; the debian family covers Debian and
Ubuntu. Both use an in-guest boot pass, so package installation does not need the libguestfs
appliance network. A shared family path and a catalog entry are not a completed live proof.

The current rootfs catalog contains these combinations:

| Catalog releases | x86_64 | ppc64le |
|---|---|---|
| Fedora 43, 44 | Present | Present |
| Rocky 9, 10; CentOS Stream 9, 10 | Present | Present |
| Rocky 8 | Present | No entry |
| Debian 12, 13 | Present | No entry |
| Ubuntu 24.04, 26.04 | Present | No entry |

The [rootfs catalog](../../fixtures/local-libvirt/rootfs_catalog.toml) owns the entries. A missing row
here makes no claim about what an upstream distributor currently publishes. Image-specific
introspection readiness is reported by `images.describe` in `capability_signals.live_drgn`;
see the [images reference](../guide/reference/images.md#imagesdescribe), rather than inferring
it from the distro family.

Recorded customization evidence:

| Configuration | Result and source |
|---|---|
| Fedora 44 x86_64/KVM and ppc64le/TCG | Built and published in the [2026-07-14 unified customization proof](../design/2026-07-13-unified-customization-boot-proof-record-1147.md). This does not establish Fedora 43. |
| Fedora 44 ppc64le/TCG with EL compatibility fixes | Built and published in the [catalog-parity proof](../design/2026-07-14-ppc64le-catalog-parity-1152-proof-record.md); component checks on EL images did not establish an EL completion. |
| Rocky 9 x86_64/KVM | Reached the completion marker and published in the [2026-07-15 EL9 proof](../design/2026-07-15-el9-customize-boot-1174-proof-record.md). This does not establish CentOS Stream or EL10. |

### Known gap — EL customize-boot on ppc64le

The cited EL9 proof records a CentOS Stream 9 package-download stall under TCG/SLIRP before
customization completed. It does not establish an end-to-end ppc64le EL build or show that only
native hardware can resolve the stall. Treat the EL ppc64le rows as available through the shared
mechanism with no successful completion in these records. Fedora 44 has the recorded ppc64le
customization proof above.

## Crash-capture methods by arch

| Method | Architecture and evidence |
|---|---|
| kdump | Implemented on both arches. ppc64le capture is recorded under TCG and native POWER9 in the proofs above. |
| host_dump | QEMU-side mechanism available on both arches; architecture-independent implementation does not establish a separate live result for every combination. |
| fadump | Local-libvirt opt-in for ppc64le; admission requirements and live limitations below. |

### Known limitation — native POWER fadump capture

A fadump profile must select `ppc64le`, opt in through `debug.fadump`, carry a `crashkernel`
reservation, and resolve to at least **4096 MiB** guest memory. Admission also requires the
host's `pseries_fadump` capability. The detector compares the discovered emulator's QEMU version
with the repository's **10.2** floor and rejects missing or failed probes. These checks do not
prove that the guest will complete a firmware-assisted capture.

The [2026-07-14 TCG record](../design/2026-07-14-ppc64le-fadump-proof-record-1151.md) reached fadump
registration and then a guest Oops. The current driver skips non-ppc64le hosts and is intended
for native POWER/KVM validation. Neither that host-architecture check nor the production
version detector enforces the accelerator; confirm KVM is actually selected for a native proof.

The [2026-07-15 RAM-floor record](../design/2026-07-15-power-native-fadump-ram-floor-1181-proof-record.md)
confirms the admission fix and a POWER10 host's QEMU/KVM prerequisites. It explicitly leaves
native crash-to-capture at 4 GiB unexecuted. It is not proof that the POWER10 guest booted or
captured successfully. Use the recorded kdump path when a demonstrated capture is required,
and report a new native fadump result with its exact runtime and fixture evidence.
