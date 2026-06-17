# ADR 0146 — Worker image ships the kernel-build toolchain + dual regression guard

- **Status:** Accepted
- **Date:** 2026-06-17
- **Deciders:** kdive maintainers

## Context

The single control-plane image (ADR-0088) carries all three entrypoints; the **worker**
runs the Build plane (ADR-0029). On the seeded `worker-local` host the warm-tree/server
build lane spawns `make` against a staged kernel source tree (`KDIVE_KERNEL_SRC`), applies
an optional `patch_ref` with `git apply`, and mirrors the warm tree with `rsync`
(`build-source-staging.md`; `providers/shared/build_host/workspace.py`).

The runtime stage of `Dockerfile` installs only:

```
gcc make binutils gdb libvirt-clients openssh-client libelf1 libdw1 zlib1g
```

A Linux kernel `make` hard-requires `flex`, `bison`, and `bc`; none are present. `git`
(both `patch_ref` application and the git-clone lane), `rsync` (warm-tree mirror), `xz`
(image/module compression), and the `libssl-dev`/`libelf-dev` headers the kernel build
compiles `scripts/sign-file`, `scripts/extract-cert`, and `objtool` against are all absent.
A toolchain probe inside the running worker (issue #499) confirmed `flex`, `bison`, `bc`,
`git`, `rsync`, and `xz` MISSING. So even a correctly-staged warm tree fails mid-`make`:
the build lane is a first-class feature that **no published image can execute**.

The gap is invisible. The only build-time check (`Dockerfile`'s
`drgn/gdb/virsh/gcc/make --version` RUN) and the gated smoke test
(`tests/image/test_image_smoke.py`) assert only the previously-shipped tools, so a missing
kernel-build tool fails late inside a job rather than at image-build time, and can regress
silently.

## Decision

1. **Ship the kernel-build toolchain in the runtime stage.** The final image installs
   `flex bison bc git rsync xz-utils libssl-dev libelf-dev` alongside the existing
   `gcc make binutils …`. These are worker-*runtime* tools — the worker spawns `make` at
   job time — so they belong in the final image, not the builder stage (whose `gcc`/
   `libvirt-dev` are build-only and correctly excluded from the runtime image).

2. **Guard the toolchain at two surfaces so the gap cannot regress silently.**
   - **Build-time `RUN` (`Dockerfile`).** Extend the existing tool-verification RUN to also
     run `flex/bison/bc/git/rsync/xz --version` and to assert the `libssl-dev`/`libelf-dev`
     packages are installed (`dpkg -s`). A missing tool fails the **image build itself**
     (and the CI `image-build` job), before any test runs — no release image can be cut
     without the toolchain.
   - **Gated smoke test (`tests/image/test_image_smoke.py`).** Assert each build *binary*
     resolves on PATH for the non-root user at the image boundary, matching the existing
     `drgn/gdb/virsh` check. The `-dev` headers are a build-time concern, not a PATH
     concern, so they stay guarded by the build-time RUN.

## Consequences

- The warm-tree `worker-local` build lane and `patch_ref` application work on the published
  image. The git-clone lane's `git` dependency is also satisfied in-image.
- The image grows by the toolchain and the two `-dev` header packages (tens of MB).
  Accepted: one image for all entrypoints is the ADR-0088 decision, and the worker — which
  needs these tools in-process — is part of that image.
- No code logic, MCP tool/schema, DB, migration, or auth change. The change is confined to
  the `Dockerfile` runtime apt set + build-time RUN assertion, the gated smoke test, and
  operator-doc honesty (naming the concrete dependencies the build path needs).
- A future BTF-enabled build config would additionally need `dwarves`/`pahole`; that is out
  of scope here (see rejected) and would be added with the config that requires it.

## Considered & rejected

- **Also ship `dwarves`/`pahole` for BTF now.** Rejected as out of scope. The shipped
  default build config (`src/kdive/build_configs/data/kdump.config`) enables
  `CONFIG_DEBUG_INFO_DWARF5`, and the required-config gate
  (`providers/shared/build_host/orchestration.py` `REQUIRED_KERNEL_CONFIG`) is satisfied by
  any one of DWARF4/DWARF5/BTF. No shipped config selects BTF, so `pahole` would be dead
  weight, and BTF couples the `pahole` version to the kernel being built. If a BTF config is
  ever shipped, a follow-up adds the `pahole` set and a config-driven guard alongside it.
- **A separate, dedicated build image.** Rejected: it contradicts ADR-0088's single-image
  decision and fragments deployment for a toolchain the worker already needs in-process.
  The larger single image is the accepted cost.
- **Install the toolchain in the builder stage / copy binaries into the final image.**
  Rejected: the kernel `make` runs at worker *runtime*, not at image-build time, so the
  tools and `-dev` headers must be present in the final image. Copying a kernel toolchain
  (with its libc and header dependencies) across stages is fragile compared with an apt
  install in the runtime stage.
- **Guard only with the smoke test (drop the build-time RUN).** Rejected: the smoke test is
  gated (needs `KDIVE_IMAGE` + docker) and runs only after a successful build. The
  build-time RUN fails the *build* on a missing tool, so a release image can never be cut
  without the toolchain; the smoke test then re-verifies the contract at the user-facing
  non-root boundary. Both layers match the existing `Dockerfile` + smoke-test pattern.
