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

- `systems.get` — read a system's status and connection details. `data.supports_snapshots`
  tells you whether the backing provider can checkpoint/restore this system.
- `systems.list` — list the systems you can see, with filters.

## Snapshots and restore

Checkpoint a fully-configured guest and roll it back in seconds — the fast path for a
panic-then-retry reproducer loop. Set up the guest once (packages, staged reproducer, armed
kdump), snapshot it, and restore between attempts instead of reprovisioning from scratch.

- `systems.snapshot` — checkpoint a READY system's disk and, by default (`include_memory=true`),
  its live RAM+CPU, under an agent-chosen `name`. A worker job; poll `jobs.wait`. Allowed during a
  live run (checkpointing mid-debug is the point). A memory capture briefly pauses the guest while
  its RAM is written, so an in-flight SSH command stalls then resumes. The system stays READY.
- `systems.list_snapshots` — list a system's checkpoints newest first, each with its `state`
  (`creating`/`available`/`failed`), `include_memory`, and `created_at`. Only an `available`
  checkpoint can be restored.
- `systems.restore` — roll a READY system back to a named checkpoint (a worker job). Refused while
  a run holds the system, while another snapshot capture/restore/delete is in progress, or while a
  debug session is attached — end the session first, then attach a fresh one after the restore.
- `systems.delete_snapshot` — delete a checkpoint, freeing its name and reclaiming disk before
  teardown (a worker job — freeing a large memory checkpoint takes time).

**Memory vs disk-only.** A RAM+CPU checkpoint (`include_memory=true`) resumes the guest exactly
where it was. A disk-only checkpoint (`include_memory=false`) is smaller and faster to take, but
restoring it rolls back the filesystem and **reboots** the guest — there is no saved CPU/RAM to
resume, so it cannot be pause-restored.

**Paused restore for a debugger.** `systems.restore(..., start_paused=true)` (a memory checkpoint
only) reverts into a suspended guest and lands the system in `paused`. Attach a gdbstub
`debug.start_session`, set breakpoints, then resume with `control.power(system_id,
action="resume")` — the only action admitted on a `paused` system, which returns it to READY.
drgn-live over SSH does not work on a paused guest (its kernel is not executing); use the gdbstub
`debug.*` tools.

Snapshots are freed when the system is torn down or its allocation is released — they never
outlive the system.

## Reaching the guest over SSH

- `systems.ssh_info` — get the SSH connection descriptor for a ready system.
- `systems.check_ssh_reachable` — probe whether a ready system's guest sshd is answering
  now (a worker job; poll `jobs.wait` and read `refs.result`).
- `systems.authorize_ssh_key` — authorize your public key so you can run commands in the
  guest over SSH.

`check_ssh_reachable` reports transport, not authorization: a `reachable=true` verdict means
the guest's sshd is answering, not that your key is authorized. It is a banner-only probe that
sends no handshake and attempts no login, so `reachable=true` is expected before you authorize a
key. If a real SSH attempt is denied with `Permission denied (publickey)`, call
`systems.authorize_ssh_key` — which both `check_ssh_reachable` and `ssh_info` point to as a next
action.

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
