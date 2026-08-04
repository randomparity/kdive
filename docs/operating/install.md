# Installing KDIVE

KDIVE's portable core runs as three processes — `server`, `worker`, `reconciler` — plus a
`migrate` one-shot, on top of operator-provided backends (Postgres, an S3-compatible object store,
and an OIDC issuer). Kubernetes runs the dedicated `lifecycle-witness` as its fourth long-running
workload. The `lifecycle-witness` is Kubernetes-only; Compose and systemd keep the three-process
core, with Compose using an operator-side lifecycle wrapper rather than a witness service. This page
covers where the code comes from, what the host needs, and the three ways to run it.

The S3-compatible object store is a **required** backend (ADR-0337): it is load-bearing for
vmcore retrieval, debuginfo staging, console parts, and artifact egress.
`server`/`worker`/`reconciler` fail startup validation with an actionable
`configuration_error` when `KDIVE_S3_ENDPOINT_URL` or `KDIVE_S3_BUCKET` is unset or blank.

The configured bucket must have bucket-wide versioning `Enabled`, with MFA Delete off and no
MinIO prefix or folder exclusions. The runtime credential needs its existing object permissions
plus `s3:GetObjectVersion`, `s3:GetBucketVersioning`, `s3:ListBucketVersions`, and
`s3:DeleteObjectVersion`. The standard
S3 versioning response does not expose MinIO's prefix/folder exclusions, so the operator must
verify that provider-specific policy separately for an external store.

The first upgrade to a version-aware image is a stop-old-first maintenance operation:

1. Quiesce every old server, worker, reconciler, writer, and deleter; wait until no object-store
   request is in flight.
2. Grant and verify the version-inspection, version-listing, and exact-version-delete IAM actions,
   then verify that MFA Delete is off and no provider-specific exclusion policy applies.
3. Enable versioning for the whole bucket and wait for the provider's documented activation
   barrier.
4. Upload a disposable probe, record the `VersionId` returned by `put-object`, then fetch that exact
   version with `aws s3api get-object --version-id "$version_id" --bucket "$bucket" --key
   "$probe_key" /tmp/kdive-version-probe`. Delete that exact version after the bytes compare equal.
   Do not start KDIVE until this exact-version read succeeds with the runtime identity.
4. Run database migrations, then start only the new version-aware image and verify readiness.

Do not run an old and new image together during this adoption. Suspending bucket versioning and a
live rollback to an image from before ADR-0524 are unsupported; recover forward with a
version-aware image. A diagnostic run of an old image must remain quiesced.

### Worker-fence authority upgrade

For a release carrying the worker-fence protocol, follow the deployment-specific authority sequence:

- **Kubernetes:** follow the [staged worker-fence upgrade procedure](
  runbooks/kubernetes-deploy.md#staged-worker-fence-upgrade).
- **Compose:** use `just compose-stop` to record old-worker termination and preserve named volumes.
  Select the new image and configuration, then use `just compose-up`. The Compose graph runs the
  migrate one-shot and, for local defaults, role bootstrap before the operator-side lifecycle
  wrapper registers the current worker. Do not invoke
  `python -m kdive.processes.compose_worker_lifecycle` directly or use raw Docker/Compose commands;
  they bypass the public lifecycle path. Compose has no persistent lifecycle-witness service.

Verify registered current incarnations and recovery-tool exposure before resuming queue processing.
Rollback cannot restore old-worker claiming after this migration; recover forward with a current
image. Raw lifecycle commands, manual finalizer removal, and database-owner or manual SQL bypasses
retain pins rather than releasing them.

### Migration 0094: full-downtime artifact index build

Migration 0094 builds a unique index over the artifact catalog inside KDIVE's atomic migration
transaction. The index build takes a write-blocking table lock, so a deployment that includes 0094
is a full-downtime maintenance operation, not a rolling upgrade:

1. Stop every old KDIVE long-running workload. On Kubernetes, stop or quiesce server and reconciler
   as appropriate, then scale workers to zero while the lifecycle-witness remains healthy.
   Wait until worker Pods and their finalizers are gone, then stop the lifecycle-witness.
   On Compose or systemd, stop only the three portable core processes. Disable restart controllers
   so an old process cannot reconnect during the migration.
2. On the target database, verify `pg_stat_activity` has no sessions from the KDIVE runtime role.
   Do not start migration while any old application session remains.
3. Run `python -m kdive migrate` once with the new image. If duplicate ownership triples make the
   unique-index build fail, inspect and repair those durable claims before retrying; the migration
   never chooses a winner or deletes data.
4. On Kubernetes, start server and reconciler as appropriate, then start and verify the
   lifecycle-witness before starting workers. On Compose or systemd, start only the new server,
   worker, and reconciler processes. Verify readiness.

For Kubernetes, complete the ordered shutdown above before the hooked upgrade that runs migration
0094. For systemd or Compose, stop only the three portable core processes before the migrate
one-shot. A normal rolling `helm upgrade` while old Pods still write is not supported for the
release containing 0094.

### Migration 0095: stop-old-first Run schema expansion

The release containing migration 0095 adds `runs.build_ref`. Older KDIVE processes use strict Run
models with `SELECT *`, so they cannot read the expanded row shape. On Kubernetes, stop or quiesce
server and reconciler as appropriate, then scale workers to zero while the lifecycle-witness remains
healthy. Wait until worker Pods and their finalizers are gone, then stop the lifecycle-witness
before migration. After migration, start server and reconciler as appropriate, then start and verify
lifecycle-witness before starting workers. On Compose or systemd, stop only the three portable core
processes and start only the new deployment afterward. Migration 0095 checks `pg_stat_activity` and
refuses to run while another client remains connected to the KDIVE database. This release is not
rolling-upgrade compatible; recover forward if migration has applied.

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

The image runs any of five commands (`server` / `worker` / `reconciler` / `lifecycle-witness` /
`migrate`) via `python -m kdive <command>`. How releases are cut and tagged is described in
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
(`resolute`, ppc64el). For the dev-loop view of the same divergences, see the
[cross-platform development guide](../development/cross-platform.md); for a from-scratch
POWER box, the [POWER host bring-up runbook](runbooks/power-host-bringup.md).

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

- **VM provisioning is arch-aware and live-proven on POWER.** A provisioned System's domain is
  rendered from the profile architecture (`kdive.domain.platform`): ppc64le uses the `pseries`
  machine type, the `hvc0` serial console (there is no `ttyS0` on pseries, so the readiness
  marker and boot cmdline target `hvc0`), and lets libvirt assign the SSH NIC's PCI slot. The
  catalog ships a `fedora-kdive-ready-44-ppc64le` image (Fedora's ppc64le Cloud Base, from the
  `fedora-secondary` tree). A ppc64le guest boots two ways, both with a live proof: cross-arch
  on an `x86_64` host under TCG emulation
  ([ADR-0342](../adr/0342-ppc64le-live-tcg-boot-proof.md)), and native under KVM-HV on a POWER
  host — the full provision → boot → crash → kdump → retrieve spine is validated on POWER9
  hardware ([ADR-0355](../adr/0355-power-native-kvm-hv-validation.md)). The
  [POWER host bring-up runbook](runbooks/power-host-bringup.md) drives a from-scratch POWER box
  to that point.

Docker Engine's official apt repository publishes `ppc64el` packages for current Ubuntu
releases, and the `postgres` and `minio/minio` test images are multi-arch (they include
`ppc64le`), so the standard Docker path and the disposable-container tests work unchanged.

Before the first start, run the provider preflight for the libvirt backend you intend to
use. The preflight reports what is missing without changing the host:

- Local provider: run `just check-local-libvirt`.
- Remote provider: run `just check-remote-libvirt HOST USER URI`.

See [local-libvirt](providers/local-libvirt.md) and
[remote-libvirt](providers/remote-libvirt.md) for what each provider needs.

### Cross-architecture guests

The local-libvirt provider can run a foreign-architecture guest (a `ppc64le` guest on an
`x86_64` host, or the reverse) under QEMU's TCG emulation. The host's **native** arch runs
under KVM; every **foreign** arch runs under **TCG**, which is emulated and roughly 10×
slower. Cross-arch guests are optional — a single-arch host needs none of this.

To enable foreign-arch guests, install the foreign arch's QEMU system emulator. The package
name is distro-specific (and matches what `scripts/check-setup-deps.sh` reports):

| distro | ppc64le emulator (`qemu-system-ppc64`) | x86_64 emulator (`qemu-system-x86_64`) |
|--------|----------------------------------------|----------------------------------------|
| Fedora / RHEL / CentOS | `qemu-system-ppc` | `qemu-system-x86` |
| Debian / Ubuntu | `qemu-system-ppc` | `qemu-system-x86` |
| Arch | `qemu-system-ppc` | `qemu-system-x86` |
| openSUSE | `qemu-ppc` | `qemu-x86` |

For example, to enable ppc64le guests on an x86_64 Fedora host: `dnf install qemu-system-ppc`.

Two diagnostics report the per-arch accelerator once the emulator is present:

- `scripts/check-setup-deps.sh` prints a cross-arch line per foreign arch — "available via
  TCG only" when its emulator is present, or the exact package to install when it is not.
- The service `doctor` (`kdivectl doctor --json`) carries a `guest_arch_accel` check whose
  `data` maps each schedulable arch to `kvm` or `tcg`, and which fails only when the host
  lacks its own native-arch emulator.

Because TCG guests boot far slower than KVM guests, the provider scales boot-readiness
deadlines for them by `KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER` (default `10.0`; must be
`>= 1.0`; set `1.0` to disable scaling). KVM guests are never scaled. Tune the multiplier
if your host's TCG throughput differs markedly from the default assumption.

## Run modes

Pick one of the three deployment shapes:

- [Docker Compose](docker-compose.md) — the app tier plus dev backends in one graph;
  the quickest way to a working endpoint for demos and evaluation.
- [Kubernetes (Helm)](kubernetes.md) — the chart deploys the three core processes, a dedicated
  lifecycle witness, and the migrate Job against external backends.
- [systemd](systemd.md) — run the processes as host services against external backends.
