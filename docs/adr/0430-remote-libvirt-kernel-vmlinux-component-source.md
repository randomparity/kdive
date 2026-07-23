# ADR 0430 — Remote-libvirt accepts a supplied KERNEL + VMLINUX component source

- **Status:** Accepted
- **Date:** 2026-07-23
- **Deciders:** kdive maintainers

## Context

Remote-libvirt advertised only two accepted component sources — `CONFIG` (`catalog`, `local`)
and `PATCH` (`local`) — reasoning "No rootfs/kernel/initrd component source is accepted: the
target boots from an operator-staged disk-image base OS." Local-libvirt accepts six kinds
(`local_libvirt/composition.py`), including `KERNEL: {local}` and `VMLINUX: {local}`. A caller who
already has a built kernel had to make kdive rebuild it on remote. This ADR is the remote parity
opt-in (#1432, part of the remote-libvirt parity epic #1423).

The stated "disk-image base OS" reason does not cover `KERNEL`/`VMLINUX`. The base image fixes the
*rootfs*, not the kernel — remote's whole purpose is iterating kernels on top of that base image.
The forces are already resolved by the existing planes and need no new mechanism:

- **The install plane is already source-agnostic.** `RemoteLibvirtInstall.install()` mints one
  presigned GET for `request.kernel_ref` and drives the in-guest helper to pull, extract, and
  add-or-replace the deterministic `kdive` grub slot with the method-conditional crashkernel
  cmdline (ADR-0078/0081/0082). It installs whatever object the ref names; nothing in the path
  assumes the bundle was *built* rather than *supplied*. The `cmdline_for` composition that sets
  the crashkernel cmdline runs upstream of install, so a supplied bundle takes the same path.
- **`local` is a worker-host path, not an upload.** `ComponentSourceKind` is
  `Literal["local", "artifact", "component-upload", "catalog"]`; `local` is `LocalComponentRef`, an
  absolute worker-host path. Accepting `component-upload` (agent upload) would be net-new capability
  for *both* providers, not parity, and is an explicit non-goal of #1423.
- **Provenance already distinguishes supplied from built.** The `provider_components` registry
  records each component's `source` (its kind — `local`/`artifact`/`component-upload`/`catalog`,
  ADR-0065). A supplied bundle is `local`; a built one is the built artifact. The recorded source
  kind is the provenance that later tells the two apart — no schema change is needed.

So the only thing that must change is what the provider *advertises as accepted*: a
component-source capability declaration, not an install-path rewrite.

## Decision

We will add `KERNEL: {local}` and `VMLINUX: {local}` to `remote_libvirt/composition.py`'s
`_component_sources()`, matching the `local` source kind local-libvirt accepts. `component-upload`
remains unaccepted for every kind, and no rootfs/initrd source is added here (the rootfs is fixed by
the operator-staged base image; the `ROOTFS` and `INITRD` entries are separate parity items in
#1423).

The change is the accepted-sources map only. `reject_unsupported_component_source` already keeps the
existing `configuration_error` shape (`provider` / `component_kind` / `source_kind` /
`accepted_source_kinds`) for any unadvertised kind/source combination, so an `artifact` or
`component-upload` KERNEL/VMLINUX ref is still rejected unchanged. No migration: the accepted-sources
map is provider-composition state, not schema, and provenance rides the existing
`provider_components.source` column.

## Consequences

- A remote caller can supply an already-built `vmlinuz+modules` bundle from a worker-host path
  instead of forcing a rebuild; it installs into the deterministic `kdive` grub slot through the
  existing artifact channel, and its provenance is the recorded `local` component source.
- `#1428` (the capability-parity guard) compares the local and remote accepted-source maps for the
  shared kinds; this change advances remote toward that parity for `KERNEL`/`VMLINUX`.
- **Live proof is deferred to the remote `live_vm` tier (#1424).** The declaration and its
  rejection shape are unit-covered; the end-to-end "supply a bundle, boot it on a remote host with
  no rebuild" proof runs on the live runner, consistent with the epic posture for the sibling
  remote ports (ADR-0428/0432).
- **Open, unenforced point (stated, not litigated here).** A supplied kernel and a supplied vmlinux
  must be consistent with *each other* for `introspect` to match the running kernel's debuginfo;
  nothing checks that consistency today (the base image's "matching vmlinux/debuginfo" is an
  operator obligation, ADR-0078/0079). This entry does not add such a check; it inherits the same
  operator obligation local carries.
