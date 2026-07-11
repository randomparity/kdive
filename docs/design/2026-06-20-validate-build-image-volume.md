# Spec: flag a guest-rootfs volume at build-host registration; point a toolchain-missing build at the diagnostic

- **Issue:** #627 (part of #618; source RUN_REVIEW.md D3)
- **ADR:** [0196](../adr/0196-validate-build-image-volume-at-registration.md)
- **Status:** Draft

## Problem

`build_hosts.register_ephemeral_libvirt` only non-blank-checks `base_image_volume`. The remote
provider stages two distinct base images:

- the **guest/boot rootfs** — `<distro>-kdive-remote-base-<ver>.qcow2` (ADR-0188 catalog:
  `*-kdive-remote-base*`), carries `qemu-guest-agent`/`drgn`/`kdump` tooling but **no kernel
  build toolchain**;
- the **build base image** — conventionally `kdive-build-*`, carries `git`/`flex`/`bison`/`make`/…

An operator who registers the guest rootfs as a build host's `base_image_volume` passes the
check; the build VM then has no `git` and the build fails minutes later with an opaque
`infrastructure_failure` ("git init failed on remote") that names neither the cause nor the
`ops.diagnostics --with-buildhost-agent` probe that already proves it.

Registration is a single authorized `INSERT` with no transport to the remote pool, so it cannot
probe the volume; the only registration-time signal is the volume name.

## Acceptance criteria (from the issue)

1. Registering a guest rootfs as a build host is **rejected or flagged**, not silently accepted.
2. A toolchain-missing build failure surfaces an **actionable pointer** to the build-host agent
   diagnostic.

## Design

### Change 1 — registration name heuristic (`mcp/tools/ops/build_hosts/register.py`)

In `_ephemeral_plan`, after the existing non-blank check, reject a `base_image_volume` whose
name **case-insensitively contains `kdive-remote-base`** (the guest/boot rootfs convention) with
a `configuration_error`:

- `reason`: the volume name matches the guest/boot rootfs convention, which carries no kernel
  build toolchain, so a build on it fails with `git: not found`.
- `detail`: stage a build base image with the toolchain (see build-source-staging /
  remote-libvirt-host-setup) and, to verify a registered host's builder, run
  `ops.diagnostics --with-buildhost-agent`.

A module constant `_GUEST_ROOTFS_VOLUME_MARKER = "kdive-remote-base"` owns the substring so the
check and the message cannot drift. The match is on `request.base_image_volume.lower()`.

Reuse the existing `_config_error(name, reason)` helper; extend it (or add a sibling) so the
richer `detail` can ride in `data` alongside `reason`, matching the `data.reason`/`detail`
shape ADR-0174 established for configuration errors. The rejection happens **before** any DB
write or audit row.

### Change 2 — build-time pointer (`providers/shared/build_host/transports/shell_transport.py`)

`ShellBuildTransport.clone` runs `git init dest` first. Today any non-zero exit →
`infrastructure_failure`. Reclassify only the **command-not-found shape**:

- **Primary:** `init.returncode == 127`. This is the reliable signal — the guest-exec transport
  runs `/bin/sh -c 'cd … && exec git init …'` and the qemu-guest-agent reports the real
  `exitcode` (`guest/build_transport.py::_exec_shell`), so a missing `git` yields rc 127; the
  SSH transport likewise propagates the remote shell's 127.
- **Backstop only:** the init stderr matches a git-not-found signal — `git` **and** a
  not-found token (`"not found"` / `"No such file"`) on the redacted tail. This covers a
  transport that fails to surface 127; it is a narrow backstop, never an override of a non-127
  rc produced by some other fault (a permission/disk fault keeps `infrastructure_failure`).

When that shape holds, raise `missing_dependency` (`ErrorCategory.MISSING_DEPENDENCY`, the
precedent for absent build tooling) with:

- message: the build host's base image is missing the kernel build toolchain (`git`);
- `details["diagnostic"] = "ops.diagnostics --with-buildhost-agent"` (a literal — not imported
  from `diagnostics/checks.py`, since the legal import direction is diagnostics→providers);
- `details["stderr"]`: the existing `redacted_tail(init.stderr, …)`.

Any other non-zero `git init` exit keeps the current `infrastructure_failure` message and
category. A helper `_is_command_not_found(result)` keeps the shape test in one place.

`CategorizedError.details` flows through `safe_error_details` into the failure envelope `data`,
so the build-job failure an operator polls carries the pointer.

## Out of scope

- Probing the volume contents at registration (no transport at registration time).
- Make-time toolchain-missing failures (`flex`/`bison`/`bc`/`make`/libelf headers) are **not**
  reclassified — they keep their existing `build_failure`. `git init` is the canonical,
  first-hit "git: not found"-class signal the issue names; the registration heuristic (change 1)
  and the build-host agent diagnostic remain the broader net for a toolchain-incomplete image.
- A new request field or MCP tool; SSH/local registration paths.
- Editing `diagnostics/checks.py` (sibling #625/#629 own it; the pointer is a string literal).
- The `runs.profile_examples`/build_hosts list registrars (sibling #626 owns those).

## Test plan

Registration (`tests/mcp/ops/test_build_hosts.py`, direct handler + injected pool):

- guest-rootfs name (`fedora-kdive-remote-base-43.qcow2`) → `configuration_error`, message names
  the toolchain mistake, `data` carries the diagnostic pointer, **no row inserted**;
- case variants (`FEDORA-KDIVE-REMOTE-BASE-43.QCOW2`) → rejected;
- legitimate build image (`kdive-build-base.qcow2`) → registers (existing happy path stays
  green);
- existing empty/non-blank rejection unchanged.

Build-time (`tests/providers/build_host/test_shell_transport.py`, `_RecordingTransport`):

- `git init` rc 127 → `missing_dependency`, `details["diagnostic"]` is the pointer (primary);
- `git init` stderr "sh: git: not found" (rc nonzero, not 127) → `missing_dependency` (backstop);
- `git init` rc 1 "permission denied" → still `infrastructure_failure` (regression guard for the
  existing `test_clone_init_non_zero_is_infrastructure_failure`; the backstop must not fire on a
  non-not-found stderr);
- ordering/other clone paths unchanged.

## Guardrails

`just lint`, `just type` (whole-tree), `just test`; full `just ci` before push. New tool-doc /
generated artifacts: none expected (no new tool, no schema change) — confirm
`tests/mcp/core/test_tool_docs.py` stays green.
