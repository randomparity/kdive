# Run `just test-linux` on macOS `podman machine` (#3074)

## Problem

`CONTAINER_ENGINE=podman just test-linux` never ran on macOS (ADR-0717 item 4). On Podman
6.1.4 (`podman machine`, libkrun), `podman info` returns the socket as a URI
(`unix:///run/podman/podman.sock`), which `podman run --volume` rejects. Without the
prefix, SELinux in the VM denies the socket (`stat: cannot statx ... Permission denied`).
The VM shares only `/Users`, `/private` and `/var/folders`; a git directory elsewhere fails
with a bare `statfs` error. A 3.6 GiB VM OOM-killed the 8 xdist workers.

## Scope

1. Podman: remove a leading `unix://` from the socket path; add
   `--security-opt label=disable` to each `run`. Docker gets no new flag.
2. Both engines: first, a probe runs the test image with the same engine flags
   and `--entrypoint test ... -r /repo/HEAD` on the read-only git mount. On failure the
   recipe exits 1, names the git directory, and says to share it or use a shared checkout.
3. Worker cap (operator option A): from the engine's `MemTotal`, keep 1.5 GiB, then 1.5 GiB
   per worker (2 and 3 workers still OOM), clamped to 1..8; print it. Unreadable memory: 8
   and a warning. Explicit `-n`/`--maxprocesses` in ARGS replaces the cap; the entry script
   stops adding `--maxprocesses=8`.

Socket gid (`0:0 660` rootless, so `tester` joins `root`) and the host gateway need no
change; the runs prove both. The recipe comment, `docs/development/cross-platform.md` and
an ADR-0717 amendment state the verified connections, the shared path, and the cap.

### Failure model

- **Actors and deployments:** a developer on macOS with Docker Desktop or `podman machine`
  (rootless or rootful connection, selected by `CONTAINER_CONNECTION`).
- **Invariants:** Docker keeps its flags and 8 workers with enough memory; the recipe tests
  a commit, never the checkout.
- **Accepted failure classes:** the probe costs one container start. Another mount failure
  also gets the shared-path message, below the engine's own error. `label=disable` applies
  only to this container, which already controls the engine (ADR-0717 Consequences). The
  cap uses total, not free, memory; a busy VM can still OOM.
- **Covered elsewhere:** none (Linux/Windows Podman, connection selection are excluded).

## Success

On this host, `just test-linux` at the branch head exits 0 with Podman rootless
(`CONTAINER_CONNECTION=podman-machine-default`) and rootful (`podman-machine-default-root`)
from a clone under a VM-shared path, and with Docker Desktop from the `/Volumes` worktree.
A Podman run from that worktree exits 1 with the Scope 2 message.

## Validation

`tests/scripts/test_test_linux_recipe.py` runs the recipe with a stub engine on `PATH` that
logs its arguments. Green: `just test-verbose tests/scripts/test_test_linux_recipe.py`.

- Scope 1: `focused-test` — Podman mounts `/run/...sock` without `unix://` and passes
  `label=disable`; Docker does not.
- Scope 2: `focused-test` — a failed probe exits 1 with the message and no suite run; a
  passed probe runs the suite.
- Scope 3: `focused-test` — 3607 MiB gives 1, 8 GiB 4, 32 GiB 8, 1 GiB 1, unreadable
  gives 8 and a warning, explicit `-n`/`--maxprocesses` drops the cap. All red before.
- Docs and ADR amendment: `task-test-not-applicable` — prose; `just docs-check` and
  `just records` check links and shape.
