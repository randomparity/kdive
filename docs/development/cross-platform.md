# Cross-platform development (x86_64 and ppc64le)

This guide owns architecture-specific development prerequisites and POWER host integration.
Use [installation](../operating/install.md#development-and-ci-toolchain) for the shared Linux
requirements and [contributing](../../CONTRIBUTING.md) for the development loop. The
[platform guide](../operating/platform-support.md) distinguishes implemented mechanisms from
recorded live proofs.

## Host prerequisites

Complete the shared prerequisites on either architecture. On POWER, complete the additions below
before installing `just`/`prek` or syncing the environment. `just check-deps` checks the host
architecture and requires `rustc` and `cargo` on ppc64le; that is a developer-tool requirement,
not a claim that every Python dependency needs a source build.

### ppc64le only

Install a Rust toolchain before the tools or project environment; source builds of dependencies
and developer CLIs need `rustc` and `cargo`. `just check-deps` enforces both on wheel-less
architectures ([ADR-0360](../adr/0360-arch-aware-rust-dep-check.md)).

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
. "$HOME/.cargo/env"
```

For `grpcio` source builds, use the system OpenSSL and zlib rather than its bundled libraries.
On Debian/Ubuntu, install their headers and export the flags before syncing the environment:

```bash
sudo apt install libssl-dev zlib1g-dev
export GRPC_PYTHON_BUILD_SYSTEM_OPENSSL=1
export GRPC_PYTHON_BUILD_SYSTEM_ZLIB=1
```

`just setup` installs the developer libraries and tools, including Go and the pinned
workflow linter, building natively on POWER. It also builds ShellCheck with Cabal because
upstream does not publish a POWER binary. These source builds make a first setup slower;
later runs reuse installed tools and compiler caches. Manual package commands above are
useful for bootstrap or operator environments; developer setup installs those headers too.

For the optional `live` dependency group, install the native `drgn` build dependencies before
`uv sync --locked --group live`. On Debian/Ubuntu:

```bash
sudo apt-get install autoconf automake libtool autoconf-archive pkgconf make gawk \
  libelf-dev libdw-dev libkdumpfile-dev
```

`libkdumpfile` must be present when `drgn` builds: an importable `drgn` without that support
cannot read kdump-compressed vmcores. The system `guestfs` binding must match the consuming Python ABI. Fixed workers use the
[installed lifecycle environment](../../deploy/systemd/README.md#fixed-live-worker-lifecycle-contract),
which is separate from the checkout/test interpreter. Update the owning host provisioning
when a native dependency is missing; copying an incompatible extension is not a repair.
A successful import alone does not prove that a real capture can be read.

Source builds can make the first setup slow; later runs reuse cached artifacts. The runtime
container's native dependency setup is maintained in the [Dockerfile](../../Dockerfile).

## macOS: run the suite in a Linux container

On macOS, a host `just test` is not regression evidence. The suite needs Linux behavior
(`/proc`, pidfd, seccomp, GNU tool output), so hundreds of tests fail on every branch.
Use `just test-linux`, which runs the same selection, hash seed and `--tb=short` output in a
Linux container at the engine's native architecture (arm64 on Apple silicon;
[ADR-0717](../adr/0717-native-arch-linux-test-container-and-aarch64-capture-filter.md)).

- `just test-linux` tests `HEAD`; `just test-linux <sha>` tests another commit. Commit first:
  uncommitted edits are not tested, and the recipe warns when the tree is dirty.
- `<sha>` must contain the `test-linux` change itself (#3072). An older commit fails inside the
  container with `justfile does not contain recipe '--maxprocesses=N'`.
- `just test-linux HEAD tests/jobs` runs only the given paths.
- The recipe works from a worktree. It mounts the git directory read-only and changes nothing
  in the checkout. A named volume, `kdive-test-linux-uv-cache`, keeps downloaded packages.
- The container runs at most 8 xdist workers, the limit for the shared test PostgreSQL lock.
  The recipe lowers the cap to fit the engine's total memory: it keeps 1.5 GiB for the
  pytest controller and the test backends and allows 1.5 GiB per worker, and prints the cap
  it chose. An explicit `-n N` or `--maxprocesses=N` in the arguments replaces the memory
  cap; `-n` stays at most 8.
- The engine must report at least 6144 MiB of total memory. Below that, the recipe stops
  before it starts a container and names the fix. A worker grows to about 1.7 GiB, and a
  3.6 GiB VM OOM-killed even one worker; 6144 MiB is the set limit. A VM reports less than
  its configured size (a 3814 MiB `podman machine` reports 3607 MiB), so set 8 GiB: run
  `podman machine stop`, `podman machine set --memory 8192`, then `podman machine start`.
- `CONTAINER_ENGINE=podman just test-linux` uses `podman machine`. A full run passed on
  Podman 6.1.4 with an 8 GiB machine, rootless and rootful, and on Docker Desktop (see the
  ADR-0717 amendment for the commits). Set `CONTAINER_CONNECTION` to choose the rootless
  (`podman-machine-default`) or rootful (`podman-machine-default-root`) connection. The
  recipe disables SELinux labels for the test container, because SELinux in the VM denies
  the engine socket otherwise.
- The engine VM must see the git directory. `podman machine` shares only `/Users`,
  `/private` and `/var/folders` by default, so a checkout on another volume fails before
  the suite with a message that names the directory. Use a checkout under a shared path,
  or re-create the machine with the volume shared (`podman machine rm`, then
  `podman machine init --volume <path>:<path>`; this drops the machine's images). Docker
  Desktop shares `/Volumes` by default.
- The container uses the engine's socket to start the test PostgreSQL and SeaweedFS
  containers, the same as `just test` on a Linux host with Docker.

## Container images

The [Compose operating guide](../../deploy/compose/README.md) owns container-stack bring-up and
worker lifecycle. The repository's configuration and publishing workflows define these choices:

| Component | POWER behavior in this repository |
|---|---|
| Postgres, MinIO and MinIO client | Compose selects the images covered by the [container architecture policy](../adr/0356-cross-platform-dev-containers.md). |
| Mock OIDC | Compose supports a local build or `KDIVE_OIDC_IMAGE` override; the host wrapper can default to a pinned mirror on emulated POWER. See [image selection](../../deploy/mock-oidc/README.md#using-the-image). The publish workflow targets `linux/amd64,linux/ppc64le`. |
| KDIVE app | Local build from the root Dockerfile; `KDIVE_IMAGE` selects a prebuilt image. Release builds target `linux/amd64,linux/ppc64le`; rolling `edge` builds target only `linux/amd64`. |
| Observability | The host-stack script starts Prometheus separately and skips Grafana on ppc64le. `--skip-obs` skips both. |

The [release guide](releasing.md#container-image-publishing) owns published-image selection and
verification. Git tags use `vX.Y.Z`; container tags use `X.Y.Z`. The ppc64le release build runs
under QEMU emulation and compiles native dependencies; it is separate from native runtime proof.
PR and rolling-edge app builds use amd64 only.

The [mock issuer README](../../deploy/mock-oidc/README.md) owns its dependency pin and rebuild
procedure. The mirror resolves architecture-neutral JVM dependencies on the build host and uses
a target-native JRE. A first local build needs its base images and Maven dependencies; it does
not work offline without those cached inputs. A published-image override needs registry access.
There is no separate native-JVM issuer step in the POWER host flow.

## Python 3.15 lane

[`python-315.yml`](../../.github/workflows/python-315.yml) evaluates CPython 3.15 on
linux/x86_64 only; it does not cover ppc64le. It runs on pull requests that change
`pyproject.toml`, `uv.lock`, either Dockerfile or the workflow itself, weekly on Mondays, and on
manual dispatch. It never gates: it is not part of the required check, and every probe continues
on error ([ADR-0817](../adr/0817-python-315-informational-lane.md)).

The project pins 3.14 exactly, so the lane's first step relaxes `requires-python` and the exact
`==` pins to `>=` in its own checkout and requires installable artifacts for linux/x86_64 on
3.15. The lock stays universal, so a forced bump can appear as a fork such as
`v3.3.4 -> v3.3.4, v3.3.9`. The edit is never committed. Every uv step shares a seven-day release-age cooldown
(`UV_EXCLUDE_NEWER="7 days"` on the job), so a release newer than a week is neither resolved nor
installed. `uv lock` downgrades a locked version younger than that and fails when a
`pyproject.toml` lower bound is younger (as after a hand bump); a fresh cp315 wheel for an old
version is held back the same week. If `pyproject.toml` no longer carries the `==3.14.*` pin, that
step fails and names the line to update. The lane then runs five probes with uv `0.13.6`: `uv lock`,
`uv sync --locked`, `ruff check --target-version py315`, `ty check --python-version 3.15` and
`just test`; `lock`, `sync` and `test` have step timeouts, so a hang shows as that probe's
failure. The job summary lists each probe's outcome, the versions `uv lock` changed, the packages
uv built from source in that run (the lane keeps no uv cache, so the list is always complete;
`libvirt-python` and the project itself always appear) and the failing test node ids; each failed
or skipped probe also raises a warning.

Contract tests that read `pyproject.toml` or `uv.lock` fail in this lane by design, because they
see the relaxed copy. A `test` failure with no failing node ids in the summary is
infrastructure (for example a test-image pull), not a 3.15 result. Read test failures in two groups: those 3.14-contract tests, and genuine
3.15 incompatibilities.

## Native POWER host integration

Use the [live-stack runbook](../operating/runbooks/live-stack.md) for provisioning, fixtures,
managed worker startup and teardown. Persistent hosts are provisioned through the repository
Ansible roles; their installed worker contract publishes the session URI in
`/etc/kdive/live-worker-libvirt.env`. Workers use fixed accounts and the installed lifecycle
socket. Do not replace that path with direct root workers or `qemu:///system`.

POWER-specific host checks:

- Native acceleration requires usable `/dev/kvm` and KVM-HV; a foreign-architecture guest uses
  TCG. See [platform support](../operating/platform-support.md#architecture-and-accelerator-tiers).
- The `libvirt_stack` role selects the native QEMU package and the distro's daemon model:
  monolithic libvirtd on Debian-family hosts, modular daemons on Red Hat-family hosts. SLIRP
  guest networking does not require the libvirt `default` network.
- On POWER, supermin may read `/boot/vmlinux-*` rather than x86's `/boot/vmlinuz-*`. The
  `live_vm_host` role makes both patterns readable by the KVM group. Reapply provisioning after
  a host kernel update; an operator-root readability check does not prove worker access.
- The native `live_vm` and spine proofs boot a guest of the host arch, so a POWER host runs them
  as ppc64le guests. The
  [live-testing runbook](../operating/runbooks/live-testing.md#live_vm-native--a-real-kernel-on-real-silicon)
  lists which proofs do and which stay x86_64-only.
- Image customization needs a workspace traversable by its QEMU identity. Use the
  [image-lifecycle permissions guidance](../operating/runbooks/image-lifecycle.md), and let
  provisioning own worker staging, console and overlay directories.

The [local preflight](../../scripts/operations/check-local-libvirt.sh) can diagnose host tools,
imports and readable kernels. `KDIVE_PYTHON` selects the checkout/CLI interpreter, where it
checks `guestfs` and `drgn` imports for build-fs and CLI kdump tooling. Separately,
`KDIVE_LIFECYCLE_PYTHON` selects the installed lifecycle worker interpreter, where it requires a
`guestfs` import when that interpreter is executable. If it is absent or not executable, the
preflight reports that the lifecycle venv is not installed and skips that probe. See
[local-libvirt host preparation](../operating/install.md#local-libvirt-host-preparation) to install
the environments and matching bindings.

It checks the configured libvirt endpoint; only local system-daemon URIs require the `libvirt` group and
active `default` network. Its success does not prove the installed session authority or a
working capture. The live-stack and live-testing checks own those deployment proofs.

### Capture-child attestation

Generate with the same target-native interpreter and installed source the worker will execute,
then perform the separate privileged installation. Re-run both commands after any Python,
loader, shared-library, or bootstrap-source update and before restarting workers. Workers only
verify the root-owned manifest; a stale manifest makes readiness fail.

For a runtime installed at the current checkout:

```bash
just build-capture-bootstrap-manifest \
  "$PWD/.venv/bin/python" \
  "$PWD/build/capture-bootstrap-manifest.json"
sudo "$PWD/.venv/bin/python" scripts/generate/build-capture-bootstrap-manifest.py install \
  --staged "$PWD/build/capture-bootstrap-manifest.json" \
  --destination /usr/share/kdive/capture-bootstrap-manifest.json
sudo test "$(stat -c '%u:%g:%a' /usr/share/kdive/capture-bootstrap-manifest.json)" = "0:0:644"
```

For an Ansible deployment, reapply `libvirt_stack` after the final runtime exists, setting
`libvirt_stack_capture_manifest_interpreter` to that runtime's Python and
`libvirt_stack_capture_manifest_source_root` to its `src` directory. The role keeps generation
and privileged installation separate. A manifest generated for a development checkout does
not attest another installed runtime.

Run the containment carrier on native POWER against the revision under test:

```bash
uv run python -m pytest tests/jobs/capture_operations/test_sandbox.py \
  tests/jobs/capture_operations/test_provider_child.py::test_child_real_provider_dispatch_writes_bounded_success_without_descendants -q
```

The fork/vfork/exec/clone/clone3 checks and empty child-process-tree check must pass on ppc64le;
emulation or skipped tests do not establish native containment. The
[live-testing guide](../operating/runbooks/live-testing.md#ppc64le-spine-on-native-power) owns the
native guest spine and fixture requirements.
