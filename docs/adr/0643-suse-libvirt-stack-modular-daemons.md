# 0643 — Use modular libvirt daemons on SUSE hosts

## Status

Proposed

- **Issue:** #2392 (sub-issue of #2388)

## Context

`libvirt_stack` provisions Debian and RedHat hosts but silently skips package and daemon setup on
SUSE. SUSE Linux Enterprise Server 15 SP7 documents modular libvirt daemons as enabled by default,
while Debian's packaged libvirt remains monolithic. The role must select one model after installing
the drivers and sockets that model requires.

The available openSUSE Tumbleweed host has no libvirt installed yet. Its repository resolves
`libvirt-daemon-qemu` to the QEMU, network, storage, node-device, and secret drivers; the
`libvirt-daemon-proxy` package supplies `virtproxyd`. The package manager accepts the remaining
host tools and Python binding capabilities in a dry run. SLES and Leap package resolution remains
unverified on a live host.

## Decision

The SUSE family uses the existing modular socket list and masks monolithic units that systemd
reports as loaded, sharing that path with RedHat. Tumbleweed's modular packages omit those units.
Its package list installs the QEMU daemon bundle, proxy, client, host tools, and Python
bindings; the architecture-specific emulator comes from the existing role map with `Suse` rows.
An unsupported OS family fails before package or service mutation with its family name and a
request for the missing package set. Debian remains monolithic.

SUSE's documented daemon model: https://documentation.suse.com/sles/15-SP7/html/SLES-all/cha-libvirt-overview.html

## Consequences

- A successful role apply can change active libvirt units on a SUSE host; the local host test must
  verify the resulting sockets and system connection.
- Package names and service behavior are tested against openSUSE Tumbleweed only. SLES and Leap
  need separate live validation before claiming host-level proof.
- The role's shared modular branch stays one path for RedHat and SUSE, so changes to its socket
  list affect both families.

## Considered & rejected

- **Use monolithic `libvirtd` on SUSE.** verified: the SLES 15 SP7 virtualization guide says
  modular daemons are enabled by default and names the QEMU and secondary daemons. Choosing the
  monolithic model would replace the distribution's default for no requirement in #2392.
- **Add a separate SUSE copy of the modular tasks.** judgment: it duplicates the same unit
  transitions and lets the RedHat and SUSE paths drift without a current behavior difference.
