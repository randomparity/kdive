# ADR 0196 — Flag a guest-rootfs volume at build-host registration and point a toolchain-missing build at the build-host diagnostic

- **Status:** Accepted
- **Date:** 2026-06-20
- **Deciders:** KDIVE maintainers

## Context

`build_hosts.register_ephemeral_libvirt`
(`mcp/tools/ops/build_hosts/register.py::_ephemeral_plan`) only non-blank-checks its
`base_image_volume`. That volume is the operator-staged base image the per-build throwaway VM
overlays (`providers/remote_libvirt/lifecycle/build_vm.py::session`), and it must carry the
kernel **build toolchain** — `git`, `flex`, `bison`, `bc`, libelf/openssl headers, `make`,
`objcopy`, `tar` (`docs/operating/runbooks/remote-libvirt-host-setup.md` §"Offloading the
from-source build").

The remote-libvirt provider also stages a *different* base image: the **guest/boot rootfs**,
conventionally named `<distro>-kdive-remote-base-<ver>.qcow2` (e.g.
`fedora-kdive-remote-base-43.qcow2`; the ADR-0188 catalog stages `*-kdive-remote-base*` across
fedora/ubuntu/rocky/bare). That image is built for booting and crash capture — it carries
`qemu-guest-agent`, `drgn`, `kexec-tools`, `kdump-utils` — but **not** the build toolchain.

An operator who registers the guest/boot rootfs as a build host's `base_image_volume` passes
the non-blank check and the row is created. The mistake only surfaces minutes later, deep in a
build, when the throwaway VM runs `git init`/`git fetch` and the missing binary returns exit
code 127 (`sh: git: not found`). Today that surfaces as an `infrastructure_failure` ("git init
failed on remote") — an opaque message that names neither the real cause (the base image has no
toolchain) nor the diagnostic that already proves it: `ops.diagnostics
--with-buildhost-agent` (`diagnostics/checks.py::EphemeralLibvirtBuildHostAgentCheck`, off by
default).

Registration has no live connection to the remote storage pool — it is a single authorized
`INSERT`. It therefore cannot *probe* the volume for a toolchain at registration time. The only
registration-time signal available is the volume name string itself.

## Decision

Two independent changes, each at the surface the operator actually sees.

### 1. Flag the documented guest-rootfs name at registration

`_ephemeral_plan` rejects a `base_image_volume` whose name matches the guest/boot rootfs
naming convention — case-insensitively containing `kdive-remote-base` — with a
`configuration_error` whose `reason` states the volume looks like a guest/boot rootfs (no build
toolchain) and whose `detail` points at the build-image staging step and the
`ops.diagnostics --with-buildhost-agent` probe. A shared literal substring
(`_GUEST_ROOTFS_VOLUME_MARKER = "kdive-remote-base"`) owns the convention so the heuristic and
its message stay in lockstep.

This is a *name heuristic*, not a probe or a guarantee. It deterministically catches the single
documented, canonical masquerade the issue describes (the provisioning base image registered as
a build base) without a remote round-trip, and it does not block a legitimately-named build
image (`kdive-build-base.qcow2`, anything not bearing the boot-rootfs marker). It is additive to
the existing non-blank check, not a replacement.

### 2. Surface the diagnostic on a toolchain-missing build failure

`ShellBuildTransport.clone` (`providers/shared/build_host/transports/shell_transport.py`) runs
`git init` as its first host command. When that command reports a *command-not-found* failure —
exit code 127, or stderr containing a `not found`/`No such file` "git"-class signal — the clone
raises a `missing_dependency` `CategorizedError` (the precedent for absent build tooling,
`images/planes/_build_common.py`) whose message names the missing build toolchain and whose
`details["diagnostic"]` is the literal `ops.diagnostics --with-buildhost-agent` pointer. Any
other non-zero `git init` exit keeps the existing `infrastructure_failure` ("git init failed on
remote") — only the command-not-found shape is reclassified, because only that shape is the
toolchain-missing cause. The `CategorizedError.details` flows through `safe_error_details` into
the failure envelope's `data`, so the build-job failure the operator polls carries the pointer.

The detection helper and the pointer literal live in `shell_transport.py`; the diagnostic
**flag string** is referenced as a literal here, not imported from `diagnostics/checks.py`
(the legal import direction is `diagnostics → providers`, never the reverse), so no
`diagnostics/checks.py` edit is required.

## Consequences

- Registering the documented guest/boot rootfs (`*kdive-remote-base*`) as an ephemeral build
  host is rejected immediately with an actionable `configuration_error`, instead of being
  silently accepted and failing minutes into the first build.
- A build that still reaches a toolchain-missing VM (a differently-named image with no `git`)
  fails with `missing_dependency` and an explicit pointer to the build-host agent diagnostic,
  turning an opaque "git init failed" into a self-correcting message.
- The registration heuristic is a name match, not a content guarantee: an operator can still
  stage a toolchain-less image under any other name and register it. That residual case is
  exactly what change 2 catches at build time. The two changes are layered, not redundant.
- No schema, DDL, or migration change. No new MCP tool or request field. No change to the
  `ssh`/`local` registration paths or to legitimately-named ephemeral registrations.
- The audited registration row, success envelope, and the SSH path are untouched; the new
  rejection happens in `_ephemeral_plan` before any DB write.

## Considered & rejected

- **Probe the volume for a toolchain at registration.** Rejected: registration holds no
  transport to the remote storage pool (it is a single authorized `INSERT`), and standing one
  up — provision a throwaway VM and exec `git --version` — would turn a metadata write into a
  minutes-long, failure-prone operation gated on remote reachability. The build-time check
  (change 2) already probes the real thing at the only point a transport exists.
- **A new required `is_build_image: true` affirmation field on the request.** Rejected: it is a
  public-contract/schema change with broad blast radius (tool docs, `runs.profile_examples`,
  every caller and test) for an operator-only tool, and an operator who mis-stages the guest
  image will set the flag anyway. The name heuristic plus the build-time pointer covers the
  documented mistake without widening the contract.
- **Reclassify *every* failed `git init` as `missing_dependency`.** Rejected: a `git init`
  failure can also be a permission or disk fault (the existing
  `test_clone_init_non_zero_is_infrastructure_failure` pins "permission denied" →
  `infrastructure_failure`). Only the command-not-found shape (rc 127 / "not found") is the
  toolchain-missing cause; narrowing the reclassification keeps the other faults correctly
  categorized.
- **Put the diagnostic pointer only in `register.py`, not on the build failure.** Rejected: the
  toolchain-missing failure can occur for an image that passed the name heuristic, so the
  build-time surface is where the operator actually lands. The acceptance criterion explicitly
  asks the *build failure* to carry the pointer.
- **Edit `diagnostics/checks.py` to own the build-time pointer.** Rejected: that module imports
  from providers, not the reverse, and sibling work (#625/#629) edits it; the pointer is a
  literal string referenced from the provider, not a `checks.py` symbol.
