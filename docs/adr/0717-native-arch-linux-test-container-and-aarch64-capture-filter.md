# 0717 — Native-arch Linux test container and the aarch64 capture filter

## Status

Accepted (2026-10-01)

## Context

On macOS, `just test` gives no usable result. The suite needs Linux behavior, so a macOS run on
2026-09-25 had about 321 failures on `main` and on a branch (#3072). The only working method was a
manual recipe in `docs/solutions/2026-09-25-macos-linux-container-test-baseline.md`. That recipe
ran an amd64 container under emulation and still had a baseline of 50 failures.

On `main` at `2c4df02fd`, Docker Desktop 29.8.1 on Apple silicon, the manual recipe gave:

- amd64 under Rosetta: `50 failed, 20191 passed` in 4 min 52 s. 36 capture-operations failures
  came from `seccomp_load failed: 125`: the capture filter does not load under emulation.
- native arm64: `28 failed, 20176 passed, 37 errors` in 3 min 29 s. The capture filter refused
  the arch (`unsupported audit architecture: aarch64`, ADR-0558), and the bootstrap manifest
  builder refused it (`unsupported architecture: aarch64`).

The operator chose native arm64 and asked for aarch64 support in the capture filter in the same
change. The operator also asked that the recipe support Docker and Podman.

## Decision

1. `just test-linux [sha]` runs the `just test` selection in a Linux container at the engine's
   native architecture. The image is `tests/container/Dockerfile`. It reuses the root
   Dockerfile's digest pins for the Python and uv base images.
2. The container clones the repository's git common directory, mounted read-only, at the
   resolved commit. Default: `HEAD`. Uncommitted changes are not tested; the recipe prints a
   warning when the tree is dirty. The clone has its own `.venv`, so the host checkout does not
   change.
3. Inside the container, the entry script calls `just test --maxprocesses=8` with
   `KDIVE_REQUIRE_DOCKER=1`. The `test` recipe passes extra arguments to pytest, so the container
   does not copy the flag list. Eight workers is the documented limit for the shared PostgreSQL
   lock.
4. `CONTAINER_ENGINE` selects `docker` (default) or `podman`. The recipe mounts the engine's API
   socket at `/var/run/docker.sock` and runs the container with `--init`. Only Docker is verified
   on macOS.
5. The capture filter supports `aarch64`. A per-arch table names the syscalls that do not exist
   on that arch. On `aarch64`, `fork` and `vfork` do not exist; glibc implements both with
   `clone`, which the filter already denies unless `CLONE_VM | CLONE_SIGHAND | CLONE_THREAD` are
   all set. All other rules are unchanged, and an arch that is not in the table still fails
   closed. The capture-bootstrap manifest and its builder accept `aarch64`.

### Amendment (2026-11-15): Podman on macOS and the memory-sized worker cap (#3074)

This amends items 3 and 4: the first Podman run on macOS showed that item 4's Podman path
did not work, and that item 3's fixed eight workers do not fit a small engine VM. The
evidence is Podman 6.1.4, `podman machine` with libkrun, 3.6 GiB and no swap, on Apple
silicon.

- `podman info` returns the socket as a URI (`unix:///run/user/501/podman/podman.sock`
  rootless, `unix:///run/podman/podman.sock` rootful), and `podman run --volume` refused it.
  The recipe removes the `unix://` prefix.
- SELinux in the VM denied the socket to the container (`stat: cannot statx
  '/var/run/docker.sock': Permission denied`). For Podman, the recipe runs the container
  with `--security-opt label=disable`. Docker gets no new flag.
- The VM shares only `/Users`, `/private` and `/var/folders`. A probe container now checks
  that the engine can read the git directory, and the recipe stops with a message that
  names the directory and the fix when it cannot.
- Eight workers, and then three, were stopped by the VM's OOM killer: a worker grows to
  about 760 MiB, and the controller, PostgreSQL and SeaweedFS take about 1.2 GiB. Item 3
  now reads: the recipe reads the engine's total memory, keeps 1.5 GiB, allows 1 GiB per
  worker, caps at 8, and prints the cap; when it cannot read the memory, it warns and uses 8.
  An explicit `-n` or `--maxprocesses` in the arguments replaces the cap, because xdist
  clamps a numeric `-n` to `--maxprocesses`.
- The socket group step and `host.docker.internal` needed no change. Under rootless Podman
  the socket is `0:0 660` in the container, so `tester` joins group `root`.

## Consequences

- A macOS developer has one regression gate. A host `just test` on macOS is not regression
  evidence.
- The gate tests commits. A developer must commit before a run to test an edit.
- The container controls the host container daemon through the socket. This is the same access
  the suite already has on a Linux host with Docker.
- Accepting `aarch64` in the capture filter is not a claim of aarch64 worker support. Worker
  host provisioning, the guest catalog, and live proofs stay `x86_64` and `ppc64le` only.
- On glibc 2.36 aarch64, the loader searches no `glibc-hwcaps` subdirectory, so the hwcaps
  drift test skips there.

## Considered & rejected

- **Keep the manual recipe.** verified: on `2c4df02fd` it left a 50-failure amd64 baseline that
  each run had to compare by hand.
- **Emulated amd64 by default.** verified: the manual recipe on `2c4df02fd` under Rosetta gave
  `seccomp_load failed: 125` for 36 capture tests and took 4 min 52 s against 3 min 29 s native.
- **Skip the capture tests on aarch64.** judgment: the operator chose to keep that coverage on
  the developer's arch.
- **Ignore any syscall that libseccomp cannot resolve.** judgment: a typo or a removed syscall
  would then drop a deny rule silently. The per-arch table keeps the omission explicit.
- **Mount the working tree.** judgment: tests and `uv sync` write into the tree, so a writable
  mount changes the macOS checkout, which #3072 forbids.
- **Test uncommitted changes with `git stash create`.** judgment: it writes objects into the
  repository; the operator chose commit-only with a warning.
- **A devcontainer.** judgment: it does not give a one-command gate; the operator excluded it.
