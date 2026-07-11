# Worker image build-toolchain completeness (#499)

- **Date:** 2026-06-17
- **Issue:** #499 — Worker image lacks the kernel-build toolchain (`flex`/`bison`/`bc`/…)
- **ADR:** [0146](../../adr/0146-worker-build-toolchain.md)
- **Status:** ready for implementation

## Problem

The control-plane image (ADR-0088) runs the worker, which executes the Build plane
(ADR-0029). On the seeded `worker-local` host the warm-tree/server build lane spawns
`make` against a staged kernel tree. The runtime stage of `Dockerfile` installs only
`gcc make binutils gdb libvirt-clients openssh-client libelf1 libdw1 zlib1g`. A Linux
kernel `make` hard-requires `flex`, `bison`, `bc` — all missing — and the lane also needs
`git` (`patch_ref` + git-clone lane), `rsync` (warm-tree mirror), `xz`, and the
`libssl-dev`/`libelf-dev` headers the kernel build compiles against. So the image is
missing tools the build lane hard-requires, and the only guards (the `Dockerfile`
`--version` RUN and the gated smoke test) assert only the previously-shipped tools, so the
gap is invisible — a missing kernel-build tool surfaces late, inside a job, not at
image-build time.

## Goal / acceptance criteria

1. The built image carries `flex`, `bison`, `bc`, `git`, `rsync`, `xz`, and the
   `libssl-dev`/`libelf-dev` headers in addition to the existing `gcc/make/binutils/gdb/…`.
2. A missing build tool fails the **image build** (not just a late job): the build-time
   `RUN` asserts each new binary's `--version` and that the `libssl-dev`/`libelf-dev`
   header files exist on disk.
3. The gated smoke test (`tests/image/test_image_smoke.py`) asserts each build binary
   resolves on PATH for the non-root user at the image boundary.
4. Operator docs that name the build prerequisite are honest about the concrete
   dependencies and which deployment supplies them.

### Verification boundary

The guards above verify **presence** of the named tools (binary `--version` exits 0; the
two `-dev` header files exist on disk), not that a kernel actually compiles. Presence is
necessary but not sufficient: a still-unlisted dependency, or a tool present but too old,
would pass every guard. No per-PR check compiles a kernel — that path is heavy (a kernel
tree + minutes of `make`) and is exercised only behind the gated `live_vm`/`live_stack`
suites, not in the `image-build` job. The named set is therefore reasoned from the
worker-local direct-boot `make bzImage` target's needs (no `modules_install`, so no `kmod`;
no initramfs assembly in-image, so no `cpio`), and the `at minimum` framing in ADR-0146 is
deliberate: if a real build later surfaces another missing tool, it is added the same way.

## Non-goals

- `dwarves`/`pahole` for BTF builds. The shipped default config uses `DEBUG_INFO_DWARF5`,
  which satisfies the required-config gate without `pahole` (ADR-0146 rejected). A BTF
  config, if ever shipped, brings its own toolchain addition.
- Any code-logic, MCP-schema, DB, migration, or auth change. None is required.
- Splitting a dedicated build image (ADR-0146 rejected — keeps ADR-0088's single image).

## Implementation plan

The work is tightly coupled (the `Dockerfile`, its build-time assertion, and the boundary
smoke test are one logical change), so it is implemented directly in-session with TDD
rather than fanned out to subagents. Three commits, guardrails green at each.

### Step 1 — Failing boundary test (red)

In `tests/image/test_image_smoke.py`, add the build binaries to the toolchain-on-PATH
assertion (a new `_BUILD_TOOLS` tuple `flex/bison/bc/git/rsync/xz`, asserted the same way as
`drgn/gdb/virsh`). Confirm red by building the **current** `Dockerfile` (pre-change), tagging
it (e.g. `kdive:pre499`), and running the smoke test against it — `flex` etc. resolve
nonzero. (The pre-change image still builds because Step 2's build-time RUN is not yet in
it.)

- Files: `tests/image/test_image_smoke.py`.
- Acceptance: the new assertion fails against the pre-change image with a "tool missing"
  message, for exactly the absent tools.

### Step 2 — Ship the toolchain + build-time guard (green)

In `Dockerfile`:
- Extend the runtime-stage `apt-get install` to add
  `flex bison bc git rsync xz-utils libssl-dev libelf-dev`, with a comment citing ADR-0146
  and why each is needed (kernel `make` hard-deps + warm-tree/patch/clone tooling +
  build-time headers).
- Extend the build-time verification `RUN` to also run
  `flex --version && bison --version && bc --version && git --version && rsync --version &&
  xz --version` and, for the `-dev` packages (which have no `--version`), assert their
  header files exist on disk: `test -f /usr/include/openssl/ssl.h` (libssl-dev) and
  `test -f /usr/include/libelf.h` (libelf-dev). A header-file existence check verifies what
  the kernel build actually consumes, rather than only `dpkg` metadata.

- Files: `Dockerfile`.
- Acceptance: `docker build` succeeds; building with any of the new packages removed fails
  at the verification RUN. The Step-1 smoke test passes against the rebuilt image.

### Step 3 — Operator-doc honesty

- `docs/operating/providers/local-libvirt.md`: replace the vague "the usual kernel build
  dependencies" with the concrete list, and note that the container worker image bundles
  them (ADR-0146/0088) while the venv-on-a-host deployment installs them.
- Files: `docs/operating/providers/local-libvirt.md` (and, if it claims the worker needs
  external tooling, a one-line note in `docs/operating/build-source-staging.md`).
- Acceptance: `just docs-links`, `just docs-paths` pass; no new doc-style-guard words.

## Guardrails

`just lint`, `just type`, `just test` for the touched test; `just docs-links`,
`just docs-paths`, `just adr-status-check` for the ADR/spec/docs; and a local
`docker build` + `KDIVE_IMAGE=kdive:dev pytest tests/image/test_image_smoke.py` to exercise
the real image (the closest local equivalent of CI's `image-build` job). Full `just ci`
before the first push.

## Rollback

Pure additive change to image contents + assertions + docs. Revert the three commits to
restore the prior (smaller, build-incapable) image. No persisted state, schema, or wire
contract is touched.
