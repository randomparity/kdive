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

### Object-store preflight

The configured bucket must have bucket-wide versioning `Enabled`, with MFA Delete off and no
MinIO prefix or folder exclusions. The runtime credential needs its existing object permissions
plus `s3:GetObjectVersion`, `s3:GetBucketVersioning`, `s3:ListBucketVersions`, and
`s3:DeleteObjectVersion`. The standard
S3 versioning response does not expose MinIO's prefix/folder exclusions, so the operator must
verify that provider-specific policy separately for an external store.

Upload a disposable probe, record the `VersionId` returned by `put-object`, then fetch that exact
version with `aws s3api get-object --version-id "$version_id" --bucket "$bucket" --key
"$probe_key" /tmp/kdive-version-probe`. Delete that exact version after the bytes compare equal.
Do not start KDIVE until this exact-version read succeeds with the runtime identity.

## Release compatibility

Capture publication protocol 4 requires a new empty Postgres database and a new versioned
object-store bucket or namespace. Migration 0113 refuses existing worker, job, capture-operation,
or artifact rows. There is no supported migration, cutover, rollback, preservation, inspection,
or cleanup path for protocol-3 state or objects. This boundary applies to every deployment mode.

For another release whose upgrade is supported, use the deployment's authority procedure:

- **Kubernetes:** the [staged worker-fence upgrade](
  runbooks/kubernetes-deploy.md#staged-worker-fence-upgrade).
- **Compose:** the [worker-fence upgrade](
  ../../deploy/compose/README.md#upgrading-worker-fence-authority).

These procedures own stopping old workers, migration, credentials, and restart order. Do not
substitute raw lifecycle commands or an old image for their evidence checks. The historical
versioning and schema decisions remain in ADRs; they do not authorize a current upgrade.

### Worker-host `depmod` is resolved without `PATH`

The release carrying explicit `depmod` resolution (#2300, shipped by PR #2313) changes how a worker
host finds `depmod`. Before it, module staging ran the bare name `depmod`, so resolution followed
whatever search path the worker process inherited — and, where the process environment carried
none, CPython's `os.defpath` fallback of `/bin:/usr/bin`. A `depmod` anywhere on that effective
path resolved. The worker now looks in exactly four directories, in this order, and runs the
absolute path it finds:

`/usr/sbin`, `/usr/bin`, `/sbin`, `/bin`

`PATH` is never consulted, and there is no environment variable that changes the list.
[ADR-0631](../adr/0631-no-operator-override-for-depmod-location.md) records that decision and holds
those four directories as the contract for where a worker host's `depmod` must live.

- **Scope** — worker hosts running the local-libvirt provider, for the host-side `depmod -b` that
  indexes an extracted kernel module tree while staging it into a guest. It applies per
  `runs.install` operation. Deployments that stage modules through the remote-libvirt guest helper
  index inside the guest and are unaffected.
- **Consequence** — a `depmod` that does not resolve fails the operation with a non-retried
  `missing_dependency` whose message names the four directories it searched. Resolution screens on
  execute permission as well as presence, so a `depmod` that sits in one of those directories but
  is not executable by the account the worker runs as reads as unresolvable and produces that same
  message: check the mode bits and the mount options before concluding the binary is missing. A
  binary that resolves but then cannot be executed at all — not an executable format, or exec
  denied — reports the same `missing_dependency` with a different message, that the resolved binary
  could not be executed, so read the message rather than the category before concluding `depmod` is
  absent. Resolution is silent when it succeeds: where a host carried a `depmod` both outside and
  inside the four directories, the one inside now runs and the outside one is ignored, so a host
  that relied on a locally built `kmod` or on a wrapper must confirm the binary now selected is the
  one it wants.
- **Recovery** — install the distribution's `kmod` package, which places `depmod` in `/usr/sbin`
  under a merged-`/usr` layout and `/sbin` under a split one. For a `depmod` built from source or
  installed to a non-FHS location, relocate or symlink it into `/usr/sbin`: the first of the four
  directories holding an executable `depmod` wins, so putting it in a later one leaves it silently
  shadowed by a packaged binary in an earlier one. It must be executable by the account the worker
  runs as, on a mount that permits execution. A symlink is supported and carries one condition:
  resolution follows the link and the target is what executes, so the target must be root-owned
  too. A link in `/usr/sbin` pointing into `/usr/local` or another group-writable path satisfies
  the search and reinstates the exposure the list excludes — `/usr/local/sbin` and
  `/usr/local/bin` are left out deliberately, because `/usr/local` is group-writable by default on
  part of the Debian family and a binary placed there would run with the worker slot account's
  authority over guest overlays. Repairing the host does not resume the install that already
  failed — issue the install again once `depmod` resolves. That failure does not drive the System
  to `failed`, so the allocation survives.

An operator whose `depmod` comes from the distribution package needs no action: on a worker exec'd
without a `PATH`, that packaged `depmod` in `/usr/sbin` is what this release makes resolvable.

## Install paths

### From source

Source development targets Linux with Python 3.14 managed by `uv`.
Install the [host prerequisites](#development-and-ci-toolchain) first, including `uv`, `just`,
and `prek`. Then clone the repository and run its setup recipe:

```bash
git clone https://github.com/randomparity/kdive
cd kdive
just setup
```

The recipe checks host dependencies, syncs the locked environment, builds the capture-bootstrap
manifest, and installs the development hooks and documentation-check dependencies. Choose a
[run mode](#run-modes) below to configure backends and start the processes.

### Container image

Released images are published to the GitHub Container Registry:

```bash
docker pull ghcr.io/randomparity/kdive:latest
```

The image runs any of five commands (`server` / `worker` / `reconciler` / `lifecycle-witness` /
`migrate`) via `python -m kdive <command>`. How releases are cut and tagged is described in
[the release process](../development/releasing.md).

## Host prerequisites

KDIVE is configured entirely through `KDIVE_*` environment variables. Every setting,
its default, and whether it is required is listed in
[the config reference](../guide/reference/config.md). At minimum the processes need a
Postgres DSN, S3 endpoint and credentials, and the three OIDC values.

### Module staging (worker hosts)

Installing a built kernel indexes its modules with the host's `depmod` (ADR-0346), so a
worker host needs `kmod` — `apt install kmod` on Debian/Ubuntu, `dnf install kmod` on
Fedora. Resolution is **not** `PATH`-based: `depmod` is looked for in `/usr/sbin`,
`/usr/bin`, `/sbin`, and `/bin` only, so a copy installed elsewhere will not be found.
The service `doctor` (`kdivectl doctor --json`) carries a `depmod_toolchain` check that
fails with the package name and those four directories when it cannot resolve one. The
published container image does not yet ship `kmod`, so a worker running from it fails
this check until
[debt 0014](../debt/0014-shipped-worker-image-has-no-depmod.md) is resolved — install
`kmod` in a derived image, or run the worker on a host provisioned by the Ansible roles,
which already declare it.

### Development and CI toolchain

Running the code from source, and reproducing the `just ci` gate, needs a build
toolchain in addition to the runtime backends. `libvirt-python` has no prebuilt wheels
and compiles against the system libvirt **and Python** headers, so those headers must be
present before `uv sync`. `just check-deps` reports gaps and may offer remediation when run
interactively; inspect its proposed actions before accepting them.

**Debian / Ubuntu:**

```bash
sudo apt install build-essential pkg-config libvirt-dev python3-dev \
  libelf-dev shellcheck shfmt git curl ca-certificates
```

**Fedora:**

```bash
sudo dnf install gcc make pkgconf-pkg-config libvirt-devel python3-devel \
  elfutils-libelf-devel ShellCheck shfmt git curl
```

On POWER, complete the [architecture-specific prerequisites](
../development/cross-platform.md#ppc64le-only), including Rust, before installing `just`/`prek`
or syncing the environment. Then install [uv](https://docs.astral.sh/uv/) and the CLIs:

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

### Architecture-specific setup

For POWER source builds, follow the [cross-platform prerequisites](
../development/cross-platform.md#ppc64le-only) before installing the tools or running `just setup`.
That guide owns Rust, native-library build flags, and platform-specific tool installation. The
[platform guide](platform-support.md) owns supported guest/distro combinations and
[cross-architecture guests](platform-support.md#cross-architecture-guests).

Before the first start, run the provider preflight for the libvirt backend you intend to
use. The preflight reports missing prerequisites:

- Local provider: run `just check-local-libvirt`.
- Remote provider: run `just check-remote-libvirt HOST USER URI`.

See the [local setup](../../examples/local-libvirt/README.md) and
[remote-libvirt](providers/remote-libvirt.md) for what each provider needs.

## Run modes

Pick one of the three deployment shapes:

- [Docker Compose](../../deploy/compose/README.md) — the app tier plus dev backends in one graph;
  the quickest way to a working endpoint for demos and evaluation.
- [Kubernetes (Helm)](runbooks/kubernetes-deploy.md) — the chart deploys the three core
  processes, a dedicated lifecycle witness, and the migrate Job against external backends.
- [systemd](systemd.md) — run the processes as host services against external backends.

## Optional fixture catalog override

The filesystem fixture catalog contains provider-scoped profiles identified by `provider`,
`name`, and `arch`. These profiles do not define kernel `CONFIG_*` or command-line policy;
use the [external-build contract](external-build-upload.md) for kernel requirements. Baseline
images are listed separately through `images.list`.

To install an editable copy of the packaged catalog:

```sh
python -m kdive install-fixtures --dest "$HOME/.config/kdive/fixtures/local-libvirt"
export KDIVE_FIXTURE_CATALOG_PATH="$HOME/.config/kdive/fixtures/local-libvirt"
```

Edit the manifest and referenced YAML files, and set `KDIVE_FIXTURE_CATALOG_PATH` consistently
for the server, worker, and reconciler. The image does not overwrite an operator-owned catalog.

For Helm, create a ConfigMap containing `manifest.yaml` and all files it references. ConfigMap
keys cannot contain `/`: put those files at the top level and change manifest paths to bare
filenames. Set `fixtures.configMapName` using the [deployment runbook](runbooks/kubernetes-deploy.md).
The chart mounts this catalog and sets its path on server, worker, and reconciler; migrate does
not load it.

Call `fixtures.validate` after the change. It returns the resolved path and available profiles,
or `configuration_error` for unusable catalog data. This checks the **server's** view; confirm
that other processes use the same files and configuration.
