# 0556 — Host workers use the distro KVM provider authority

## Status

Accepted (2026-08-12)

## Context

ADR-0555 isolates each fixed host worker and grants access to the dedicated session-libvirt
socket and provider directories. The native live proof showed that this is insufficient for the
local-libvirt provider on supported KVM hosts. Libguestfs builds its appliance from a host kernel,
which Debian-family hosts protect as `root:kvm` mode `0640`, and QEMU acceleration uses the
`root:kvm` `/dev/kvm` device. A worker without the distro's `kvm` group cannot read that kernel or
use KVM even though it can reach the session-libvirt socket.

Changing host kernels or `/dev/kvm` to world-accessible modes would weaken the distro's existing
provider boundary. Creating a KDIVE-specific substitute group would require duplicating the
distro's kernel and device authority through ACLs or device rules.

## Decision

Fixed host worker accounts retain unique primary groups and receive exactly two supplemental
provider groups: `kdive-live-libvirt` and the host distro's existing `kvm` group. They remain
excluded from `kdive-live-control`, sudo, and Docker groups. The `kvm` group is a prerequisite of
the supported local-libvirt host contract; standalone installation fails before activation if it
is absent.

Provisioning keeps host kernels `root:kvm` mode `0640` and uses the distro-owned `/dev/kvm`
authority. It verifies every fixed worker can read each discovered host kernel and read and write
`/dev/kvm`. This decision supersedes only ADR-0555's statement that worker accounts share only the
session-libvirt socket and provider directories. Its lifecycle, credential, witness, and cleanup
decisions remain unchanged.

## Consequences

A process that compromises a fixed worker can ask the kernel KVM API to create virtual machines
and can read the host kernel images available to libguestfs. It cannot control the lifecycle
witness, use sudo or Docker, or act through another worker's primary group.

Supported host provisioning must supply the distro's standard KVM device and group before the
fixed lifecycle is installed. The account verifier fails when a worker lacks either provider
group, gains a forbidden group, cannot read a host kernel, or cannot use `/dev/kvm`.

## Considered & rejected

- **Make host kernels and `/dev/kvm` world-accessible.** This discards the distro's existing KVM
  authorization boundary for unrelated local accounts.
- **Add per-worker ACLs or custom device rules.** This duplicates the provider authority and adds
  kernel-upgrade and device-lifecycle convergence that the distro's `kvm` group already owns.
- **Run libguestfs or workers as root.** This grants filesystem and process authority unrelated to
  local-libvirt and defeats the fixed unprivileged worker boundary.
