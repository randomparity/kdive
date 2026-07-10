# Installing KDIVE

KDIVE runs as three processes — `server`, `worker`, `reconciler` — plus a `migrate`
one-shot, on top of operator-provided backends (Postgres, an S3-compatible object store,
and an OIDC issuer). This page covers where the code comes from, what the host needs, and
the three ways to run it.

## Install paths

### From source

Clone the repository and install the locked dependency set with [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/randomparity/kdive
cd kdive
uv sync
```

This gives you a `.venv` with the `kdive` package and the `just` recipes used throughout
the docs. Run a process directly with `uv run python -m kdive server`.

### Container image

Released images are published to the GitHub Container Registry:

```bash
docker pull ghcr.io/randomparity/kdive:latest
```

The image runs any of the four entrypoints (`server` / `worker` / `reconciler` / `migrate`)
via `python -m kdive <command>`. How releases are cut and tagged is described in
[the release process](../development/releasing.md).

### PyPI

A PyPI distribution is planned but not yet published. Use the source or container install
until it lands.

## Host prerequisites

KDIVE is configured entirely through `KDIVE_*` environment variables. Every setting,
its default, and whether it is required is listed in
[the config reference](../guide/reference/config.md). At minimum the processes need a
Postgres DSN, S3 endpoint and credentials, and the three OIDC values.

### Development and CI toolchain

Running the code from source, and reproducing the `just ci` gate, needs a build
toolchain in addition to the runtime backends. `libvirt-python` has no prebuilt wheels
and compiles against the system libvirt **and Python** headers, so those headers must be
present before `uv sync`. `just check-deps` reports any gaps without installing anything.

**Debian / Ubuntu:**

```bash
sudo apt install build-essential pkg-config libvirt-dev python3-dev \
  libelf-dev shellcheck shfmt nodejs npm git curl ca-certificates
```

**Fedora:**

```bash
sudo dnf install gcc make pkgconf-pkg-config libvirt-devel python3-devel \
  elfutils-libelf-devel ShellCheck shfmt nodejs npm git curl
```

Then install [uv](https://docs.astral.sh/uv/) and the `just` / `prek` CLIs:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install rust-just
uv tool install prek
```

The full `just ci` gate additionally exercises Docker (disposable Postgres/MinIO via
testcontainers). Tests that need Docker skip cleanly when it is absent unless
`KDIVE_REQUIRE_DOCKER=1` is set. Install Docker Engine from your distribution, or from
[Docker's official apt repository](https://docs.docker.com/engine/install/ubuntu/) on
Ubuntu, and add your user to the `docker` group.

### ppc64le (POWER) notes

On architectures without prebuilt Python wheels or tool release binaries — notably
`ppc64le` — the components above build from source instead. This is mostly automatic once
the toolchain is present, but four extra requirements apply. Validated on Ubuntu 26.04
(`resolute`, ppc64el).

- **A Rust toolchain is required.** `pydantic-core` (a `uv sync` dependency) and the
  `just` / `prek` CLIs have no `ppc64le` wheels or release binaries, so they compile from
  source with `cargo`. Install the toolchain via [rustup](https://rustup.rs) so
  `~/.cargo/bin` is on `PATH`:

  ```bash
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
  . "$HOME/.cargo/env"
  ```

- **`grpcio` must build against the system OpenSSL.** `grpcio` (an OpenTelemetry
  dependency) has no `ppc64le` wheel, and its vendored BoringSSL does not recognize the
  architecture (`#error "Unknown target CPU"`), so the default source build fails. Install
  the system OpenSSL headers and set the documented build flag so `uv sync` uses them:

  ```bash
  sudo apt install libssl-dev zlib1g-dev
  export GRPC_PYTHON_BUILD_SYSTEM_OPENSSL=1
  export GRPC_PYTHON_BUILD_SYSTEM_ZLIB=1
  uv sync --locked
  ```

  `uv sync --locked` validates the source-distribution hash, not the build flags, so this
  stays lock-faithful. Ubuntu 26.04 ships OpenSSL 3.5.5, which has full POWER support.

- **`actionlint` must be built from Go source for `just lint-workflows`.** `actionlint-py`
  only downloads a prebuilt `actionlint`, and upstream publishes no `ppc64le` binary, so it
  fails to install. `actionlint` is pure Go and builds natively; the recipe uses a PATH
  `actionlint` on `ppc64le`. Install Go and build it:

  ```bash
  sudo apt install golang-go
  go install github.com/rhysd/actionlint/cmd/actionlint@v1.7.12
  export PATH="$(go env GOPATH)/bin:$PATH"   # persist in your shell profile
  ```

- **Expect long first builds.** Wheel-less native dependencies compile the first time
  they are installed and are then cached. `grpcio` and `aws-lc-sys` (pulled in by the
  `prek` and `zizmor` Rust CLIs) are large C/C++/Rust builds that can each take from tens
  of minutes to over an hour on a POWER core budget. Subsequent runs reuse the cached
  artifacts. On high-core hosts, disabling LTO speeds the `zizmor` build markedly:
  `export CARGO_PROFILE_RELEASE_LTO=false CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16`.

- **Cap the test worker count on high-core hosts.** `just test` runs `pytest -n auto`,
  and the suite starts a Postgres + MinIO container *per worker*. On a machine with many
  cores (e.g. 32), `auto` starts dozens of containers at once and overwhelms the Docker
  daemon (container-start and connection-pool timeouts). Cap the workers — this is a
  general high-core knob, not ppc64le-specific:

  ```bash
  export PYTEST_XDIST_AUTO_NUM_WORKERS=6   # tune to taste; 6-8 is comfortable
  ```

- **VM provisioning is arch-aware but unproven on POWER.** A provisioned System's domain is
  rendered from the profile architecture (`kdive.domain.platform`): ppc64le uses the `pseries`
  machine type, the `hvc0` serial console (there is no `ttyS0` on pseries, so the readiness
  marker and boot cmdline target `hvc0`), and lets libvirt assign the SSH NIC's PCI slot. The
  catalog ships a `fedora-kdive-ready-44-ppc64le` image (Fedora's ppc64le Cloud Base, from the
  `fedora-secondary` tree). This path is unit-tested but has no live end-to-end proof: the POWER
  host exposes `/dev/kvm`, but `qemu-system-ppc64`, `libvirt`, and `virt-builder` are not yet set
  up, so booting a ppc64le guest has not been validated.

Docker Engine's official apt repository publishes `ppc64el` packages for current Ubuntu
releases, and the `postgres` and `minio/minio` test images are multi-arch (they include
`ppc64le`), so the standard Docker path and the disposable-container tests work unchanged.

Before the first start, run the provider preflight for the libvirt backend you intend to
use. The preflight reports what is missing without changing the host:

- Local provider: run `just check-local-libvirt`.
- Remote provider: run `just check-remote-libvirt HOST USER URI`.

See [local-libvirt](providers/local-libvirt.md) and
[remote-libvirt](providers/remote-libvirt.md) for what each provider needs.

## Run modes

Pick one of the three deployment shapes:

- [Docker Compose](docker-compose.md) — the app tier plus dev backends in one graph;
  the quickest way to a working endpoint for demos and evaluation.
- [Kubernetes (Helm)](kubernetes.md) — the chart deploys the three processes and the
  migrate Job against external backends.
- [systemd](systemd.md) — run the processes as host services against external backends.
