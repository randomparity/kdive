# Run `just test-linux` on macOS `podman machine` (#3074)

## Problem

`CONTAINER_ENGINE=podman just test-linux` never ran on macOS (ADR-0717 item 4). On Podman
6.1.4 (`podman machine`, libkrun), `podman info --format '{{.Host.RemoteSocket.Path}}'`
returns a URI (`unix:///run/podman/podman.sock`), and `podman run --volume` rejects it
(`invalid option type`). Without the prefix, the entry script stops at `stat: cannot statx
'/var/run/docker.sock': Permission denied`: SELinux in the VM labels the socket. A third stop is host-specific: the VM
shares only `/Users`, `/private` and `/var/folders`, so a git directory on another volume
fails with `statfs <path>: no such file or directory`, which names no fix.

## Scope

Change only the `test-linux` recipe in `justfile`:

1. Podman branch: remove a leading `unix://` from the socket path.
2. Podman branch: add `--security-opt label=disable` to `run`. Docker gets no new flag.
3. Both engines: before the suite run, a probe container mounts the git common directory
   read-only. On failure the recipe exits 1 with `test-linux: <engine> cannot mount <dir>`
   and the fix: share the path with the engine VM, or use a checkout under a shared path.

`tests/container/run-suite.sh` and the host gateway stay unchanged. Under rootless Podman
the socket is `0:0 660` in the container, so `tester` joins group `root`; the runs prove it.
Docs (`docs/development/cross-platform.md`, an ADR-0717 amendment) state the verified
connections, the shared-path requirement, and the run results.

### Failure model

- **Actors and deployments:** a developer on macOS with Docker Desktop or `podman machine`
  (rootless or rootful connection, selected by `CONTAINER_CONNECTION`).
- **Invariants:** the Docker run keeps its flags; the recipe still tests a commit and never
  writes into the checkout.
- **Accepted failure classes:** the probe costs one extra container start per run. A mount
  failure from another cause also gets the shared-path message; the engine's own error line
  prints above it. `label=disable` disables SELinux separation only for this test container,
  which already controls the engine through its socket (ADR-0717 Consequences).
- **Covered elsewhere:** none (Linux/Windows Podman and connection selection are excluded).

## Success

On this host, `just test-linux` at the branch head exits 0 with Podman rootless
(`CONTAINER_CONNECTION=podman-machine-default`), Podman rootful
(`podman-machine-default-root`), and Docker Desktop. A run from a checkout on an unshared
volume exits 1 with the message in Scope item 3.

## Validation

New `tests/scripts/test_test_linux_recipe.py` runs the recipe with a stub engine on `PATH`
that logs its arguments. Green: `just test-verbose tests/scripts/test_test_linux_recipe.py`.

- Socket URI (Scope 1): `focused-test` — the Podman `run` mounts `/run/x.sock`, not
  `unix:///run/x.sock`; red today with the URI in the mount.
- SELinux label (Scope 2): `focused-test` — Podman `run` has `label=disable`, Docker `run`
  does not; red today for Podman.
- Shared-path probe (Scope 3): `focused-test` — a stub probe failure exits 1 with the
  message and starts no suite run; red today (no probe).
- Live behavior: the runs in Success.
- Docs and ADR amendment: `task-test-not-applicable` — prose; `just docs-check` and
  `just records` cover links and record shape.
