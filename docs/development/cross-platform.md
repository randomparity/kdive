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

The workflow-lint recipe uses a PATH `actionlint` on ppc64le. Install Go and build the version
pinned by the repository's workflow tooling:

```bash
sudo apt install golang-go
go install github.com/rhysd/actionlint/cmd/actionlint@v1.7.12
export PATH="$(go env GOPATH)/bin:$PATH"
```

For the optional `live` dependency group, install the native `drgn` build dependencies before
`uv sync --locked --group live`. On Debian/Ubuntu:

```bash
sudo apt-get install autoconf automake libtool autoconf-archive pkgconf make gawk \
  libelf-dev libdw-dev libkdumpfile-dev
```

`libkdumpfile` must be present when `drgn` builds: an importable `drgn` without that support
cannot read kdump-compressed vmcores. For the system `guestfs` binding, follow the
[worker-venv instructions](../operating/runbooks/four-method-live-run.md#wire-the-worker-venv-drgn--libguestfs),
including the Python ABI match and Debian/Ubuntu `dist-packages` path. A successful import
alone does not prove a real capture can be read.

Source builds can make the first setup slow; later runs reuse cached artifacts. The runtime
container's native dependency setup is maintained in the [Dockerfile](../../Dockerfile).

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
- Image customization needs a workspace traversable by its QEMU identity. Use the
  [image-lifecycle permissions guidance](../operating/runbooks/image-lifecycle.md), and let
  provisioning own worker staging, console and overlay directories.

The [local preflight](../../scripts/operations/check-local-libvirt.sh) can diagnose host tools,
imports and readable kernels, using `KDIVE_PYTHON` to select the worker interpreter. It also
checks the older system connection and default network, so its success is not proof of the
installed session authority or a working capture. The live-stack and live-testing checks own
those deployment proofs.

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
