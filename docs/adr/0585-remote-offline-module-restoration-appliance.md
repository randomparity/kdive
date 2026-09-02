# 0585 — Remote offline module restoration uses a confined appliance

## Status

Accepted (2026-08-28)

## Context

ADR-0583 requires remote-libvirt external boot to install a release-qualified module tree into a
System root disk, preserve the replaced tree for crash-consistent recovery, and restore it before
the System returns to its disk/GRUB baseline. The current remote install path runs a helper inside
the live guest. It cannot provide the stopped-guest mutation and recovery boundary that external
boot requires.

The worker reaches the remote host only through its mutually authenticated libvirt connection. A
worker-host libguestfs process cannot generally open a disk discovered through a remote libvirt
domain because the remote host path is not necessarily present on the worker. Adding SSH or a new
general-purpose host agent would widen the operator contract and command surface. Editing a disk
while its guest or another editor can write it risks filesystem corruption, so the operation needs
one exclusive, inspectable attachment owner.

The mechanism must work for x86_64 and ppc64le Systems without executing guest binaries. It must
also bound archive work and temporary storage, preserve secrets by reference, survive worker retry,
and leave enough durable evidence to distinguish a completed capture, mutation, restoration, and
teardown from an interrupted attempt.

## Decision

Remote-libvirt performs offline module capture, installation, and restoration in a transient,
network-disabled appliance domain on the remote libvirt host. The appliance is a versioned,
operator-provisioned image selected for the System architecture. It exposes only a fixed operation
protocol; it is not a shell, package manager, arbitrary file editor, or user-script executor.
KDIVE communicates through libvirt-owned console and block-device operations, not SSH.

Before starting the appliance, the worker proves that the System domain is shut off and holds the
provider-neutral mutation authority selected by #2113. This mechanism cannot be implemented until
that decision is accepted. Every KDIVE lifecycle path must honor that authority through verified
appliance teardown. Under it, the worker enumerates active and inactive domain definitions and
requires the root volume to be referenced only by its owning, shut-off System. It then creates one
attempt-scoped transient appliance with auto-destroy semantics and attaches:

- the exact System root volume read-write, as the sole mutable guest disk;
- a read-only, content-addressed module-source volume built from the validated ADR-0583 module
  obligation; and
- a newly created, capacity-bounded scratch volume for the captured prior tree and result record.

The appliance discovers the root filesystem from a closed, versioned operation document. It never
accepts a host path, device name, mount option, command, environment variable, or destination path
from the caller. The only mutable destination is the no-follow
`lib/modules/<validated-release>/` directory selected under the mounted root. The source tree has
already passed ADR-0583 topology and manifest validation; the appliance repeats the manifest,
member-count, and uncompressed-byte checks before mutation. It uses directory-relative no-follow
operations, stages the replacement beside the destination, runs only its built-in `depmod` for the
validated release, and computes `module-installed-tree-v1`. It never executes a binary from the
System disk or source volume. `depmod` reads foreign-architecture module metadata as data, so the
appliance executable matches the remote hypervisor architecture, not the installed kernel's
architecture. The cross-architecture ppc64le live proof in
`docs/design/2026-07-13-ppc64le-kdump-proof-record-1148.md` records host-side `depmod` indexing a
ppc64le module tree under an x86_64 appliance.

### Durable capture and identity

The first mutation attempt copies the complete existing release directory, including an explicit
absent marker when no directory exists, into the scratch volume. Capture is bounded by the same
200,000-entry and 8 GiB uncompressed-content ceilings as ADR-0583. The scratch volume has a fixed
10 GiB capacity per attempt: unit bytes, scope one System/Run recovery point, no reference clock.
Crossing any entry, byte, or volume limit stops before replacement with terminal
`INSTALL_FAILURE`; recovery is to reduce the existing module tree or rebuild the System baseline.

The appliance writes a canonical `remote-module-recovery-v1` result containing the System and Run
UUIDs, plan identity, root-volume key and libvirt volume identity, validated release, source and
installed manifests, capture manifest or absent marker, appliance image digest, operation nonce,
and completion state. Core stores the result and the exact scratch-volume reference as part of the
ADR-0583 recovery point before activation. A recovery point is reusable only when every identity
field and both volume identities match. The scratch volume remains attached to no domain between
operations and is retained until recovery or successful baseline commitment makes it unnecessary.

### Retry and restoration

Operation identity is `(system_id, run_id, plan_identity, operation_nonce, phase)`. Mutation uses
three nonce-qualified names on the root filesystem: destination `D`, staged replacement `N`, and
displaced destination `O`. `N`, `D`, and `O` are on the same filesystem. The appliance makes the
capture and its manifest durable on scratch and writes durable `captured`; writes durable
`staging-intent`; creates `N`, makes its contents durable, and writes durable
`replacement-ready`; renames `D` to `O` when the capture is not absent and syncs the parent;
renames `N` to `D`, syncs the parent, and verifies the installed manifest; then writes durable
`installed`. Only after `installed` is durable may a retry remove `O`.

Inside the appliance, making data or a phase durable means fsyncing each changed file, fsyncing its
changed directories, and issuing `syncfs` for the root and scratch filesystems. Intermediate phase
checkpoints do not shut down the appliance. Before the worker persists a terminal result, the
appliance repeats `syncfs`, unmounts both filesystems, powers off cleanly, and the worker observes
the transient domain shut off and all three volumes detached. Libvirt has no storage-volume flush
operation, so this decision does not require one. Failure of any appliance flush, unmount, clean
shutdown, or detach observation is incomplete, never success; retry trusts only phases and
manifests that survived reopening the volumes.

A retry reads the durable scratch phase and the manifests of every present `D`, `N`, and `O` before
writing. `captured` permits only the original `D` and no `N` or `O`. `staging-intent` permits the
original `D`, no `O`, and either absent `N` or a nonce-owned partial `N`; it removes that `N`, syncs
the parent, and rebuilds it from the already verified source. `replacement-ready` permits the
original `D` plus complete staged `N`, absent `D` plus original `O` and complete `N`, or installed
`D` plus original `O`. Those states respectively restart the rename sequence, finish `N` to `D`,
or verify and record `installed`. The absent-capture form follows the same states without `O`.
`installed` requires installed `D` and permits only removal of a matching `O`. Any other name,
phase, or manifest combination is a recovery conflict; the appliance performs no further write and
core parks the System on the ADR-0583 conflict path.

Restoration uses the same appliance, names, ordering, and retry table with captured and installed
roles reversed. It first verifies all identity fields, the root volume, and the captured manifest;
stages the captured tree as `N`, or records the absent-capture removal operation; and writes durable
`restore-ready` before renaming. Durable `restored` requires the exact captured `D` or verified
absence, a synced parent, and no unclassified name. Only that result permits teardown of the
scratch volume. Missing, unreadable, mismatched, or over-limit recovery material fails closed and
retains the scratch volume for diagnosis; System teardown remains the terminal escape described by
ADR-0583.

### Isolation, teardown, and redaction

The appliance has no network interface, host filesystem share, graphics device, persistent domain
definition, or device beyond its three allowlisted volumes and a bounded console. Its libvirt
domain name, disk aliases, operation nonce, and volume references are deterministic from durable
identities. The worker rejects any pre-existing domain or attachment whose immutable definition
does not match. The #2113 authority serializes all KDIVE attachment and System-start paths across
the check and mutation window. A libvirt administrator can bypass KDIVE and race attachment; that
privileged operator interference is outside the trust boundary, as established by #2105, and is
not reported as an ordinary recoverable conflict.

Normal completion destroys the appliance, verifies it absent, detaches all volumes, and then
deletes only attempt-scoped staging volumes whose durable owner and digest match. Reaper logic
applies the same identity checks after worker death; it never detaches a root disk from an active
System or deletes an unowned volume.

Object-store credentials, libvirt credentials, and presigned URLs never enter the appliance. The
worker streams bounded source and result volumes through existing libvirt storage operations and
keeps capability URLs inside the existing redaction scope. Console output is passed through the
redactor before persistence and is limited to operation phase, stable error code, identities, byte
counts, and manifests. File content, directory listings, credentials, host paths, and raw tool
output are never returned or persisted.

### Amendment (2026-09-02): the durable owner is the volume name (#2157)

This is an amendment because
[ADR-0588](0588-remote-module-volume-ownership-lives-in-the-volume-name.md) supersedes only the
unnamed ownership channel behind two claims above, and leaves the rest of this decision in force.
The claims it qualifies are "deletes only attempt-scoped staging volumes whose durable owner and
digest match" and "Reaper logic applies the same identity checks after worker death" in the
preceding section: this record never named the channel carrying those durable identities, and the
libvirt storage-volume `<metadata>` element the implementation used does not persist. ADR-0588
carries the owner tuple in the volume name instead, and requires the durable attempt row to be
written before any of an attempt's volumes are created, which qualifies "Core stores the result
and the exact scratch-volume reference as part of the ADR-0583 recovery point before activation"
under *Durable capture and identity* by putting one earlier durable write ahead of it. ADR-0588
also states, rather than changes, what the preceding section requires of reaping: the set a sweep
retains against is the set of un-discharged recovery obligations, so a scratch volume stays
"retained until recovery or successful baseline commitment makes it unnecessary" even though its
writing attempt has ended. The appliance, isolation, retry, restoration, and teardown decisions
here remain in force.

## Consequences

- Remote offline mutation gains one operator-provisioned appliance image per supported hypervisor
  architecture and attempt-scoped source/scratch volumes. Provisioning, image-digest validation,
  capacity accounting, reaping, and native live proofs are implementation work, not part of this
  ADR-only change.
- The design preserves the existing remote-libvirt trust boundary: the worker needs libvirt and
  object-store access but no SSH or general remote command service.
- x86_64 and ppc64le use the same fixed protocol and filesystem operations. Each appliance runs
  natively on its remote hypervisor; no guest executable or cross-architecture emulation is used.
- Recovery evidence consumes up to 10 GiB per in-flight external boot and remains allocated through
  recovery conflicts. Admission and reaping must account for that retained capacity.
- A libvirt connection loss destroys the transient appliance but does not erase its scratch volume
  or result record. Retry therefore resumes from durable disk evidence instead of trusting worker
  memory or console output.

## Considered & rejected

- **Run libguestfs or `virt-customize -c <remote-uri> -d <domain>` on the worker.** verified:
  libguestfs `guestfish(1)` documents that domain disks must be locally accessible and that a remote
  libvirt connection commonly fails when the worker lacks the same device paths. KDIVE's remote
  volumes are host-local libvirt storage references, so this does not provide the required access.
- **Install libguestfs on the remote host and expose it through SSH or a new host service.**
  judgment: this adds a general remote execution or new authenticated service boundary solely to
  perform three fixed filesystem operations; the confined libvirt appliance uses the provider's
  existing control plane and exposes less authority.
- **Keep using the running guest helper.** verified: `RemoteLibvirtInstall.install` invokes the
  guest agent helper while the guest is running, while ADR-0583 requires capture and replacement
  before external activation and restoration before disk/GRUB recovery. The live path cannot be
  the sole writer of the root disk it is booted from.
- **Attach the stopped System root volume to a long-lived maintenance VM.** judgment: a shared
  mutable domain couples tenants and attempts, enlarges the attachment and cleanup state space, and
  makes stale credentials and cross-guest residue persistent. One transient appliance per attempt
  keeps ownership and teardown inspectable.
- **Mount the remote filesystem directly on the worker with NBD, SSHFS, or FUSE.** verified:
  libguestfs `guestfs(3)` supports selected remote-storage protocols, but KDIVE currently exposes
  only libvirt volume operations and no NBD export or host-filesystem endpoint. Adding one would be
  a second remote storage boundary; FUSE is also explicitly discouraged by libguestfs for
  performance.
- **Do nothing and reject remote external boot.** judgment: it does not meet #2105's accepted
  external-boot outcome or ADR-0583's provider recovery contract.
