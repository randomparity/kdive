# systems toolset

A system is the target machine a run builds, installs, and boots on. Reach for these after
you hold an allocation (see the allocations stage in the index) to define, provision, and
reach the target. For exact parameters, types, and return schema, read each tool's own
description.

## Defining and provisioning

- `systems.define` — describe the target system (shape, image, profile) without
  provisioning it yet.
- `systems.provision` — define and provision a target in one step.
- `systems.provision_defined` — provision a system you already defined.
- `systems.profile_examples` — fetch ready-made system-profile templates to start from.
- `systems.reprovision` — rebuild a system back to a clean baseline (for example, to
  refresh a local-libvirt rootfs).
- `systems.teardown` — destroy a system and release its host resources.

Some debug and live-introspection capabilities are bound at provision: the profile's
`debug` flags (the gdb stub and crash-preserve) and the live-ssh credential cannot be
added to a ready system. Set them in the profile before `systems.provision`, or use
`systems.reprovision` (which rebuilds and reboots) to change them later. See the
provisioning-for-debugging notes in the investigation index.

## Inspecting

- `systems.get` — read a system's status and connection details.
- `systems.list` — list the systems you can see, with filters.

## Reaching the guest over SSH

- `systems.ssh_info` — get the SSH connection descriptor for a ready system.
- `systems.check_ssh_reachable` — probe whether a ready system's guest sshd is answering
  now (a worker job; poll `jobs.wait` and read `refs.result`).
- `systems.authorize_ssh_key` — authorize your public key so you can run commands in the
  guest over SSH.

Once authorized you have **root** in the guest, and kdive never holds the private key. The
guest is yours to customize: the guest package manager is your own — install tracers,
compilers, and stress tools at runtime (`apt install trace-cmd`) rather than concluding a
capability is missing. Mind disk headroom, since toolchains and captures consume guest disk.

Runtime installs need the guest to reach its distro mirrors. On **local-libvirt** the guest
has **no outbound egress by default** (the NIC is loopback-forwarded for SSH with QEMU
`restrict=on`), so `dnf`/`apt install` fails to resolve any host until the **operator** enables
egress for that resource (`guest_egress = true` on the `[[local_libvirt]]` block in the operator's
systems inventory — not a per-request knob). Ask your operator to enable it, or use an image that
already bakes the toolchain you need. On **remote-libvirt** the operator-staged base image and host
network already provide egress.
