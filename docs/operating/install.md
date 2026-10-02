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
Bootstrap with Git, Bash >= 4.4, `uv`, and `just`, and put `uv tool dir --bin` on `PATH`.
Then clone the repository and run its developer setup recipe:

```bash
git clone https://github.com/randomparity/kdive
cd kdive
just setup
```

The recipe installs the complete developer toolchain before syncing the locked environment,
building the capture-bootstrap manifest, installing Ansible collections, and installing and
running the commit hooks. Native libraries and tools come from distribution packages (root
or sudo required); pinned user tools go into `uv tool dir --bin`. These include `prek`,
ShellCheck, shfmt, actionlint, Helm, gitleaks, and promtool. Docker and Compose are required
for developer setup, as are the compiler and native headers exercised by the tests.
Go tools build natively, and POWER builds ShellCheck with Cabal when no release binary exists.

An installation failure or inaccessible Docker daemon fails setup before dependency sync.
Setup preserves an existing Docker installation and does not change group membership or
start services: follow the reported access/startup remedy, then rerun setup. Distributions
without a known Docker engine package require an operator-configured engine or reachable
Docker context. The full installer runs on Linux; use a Linux development VM from macOS.

`just check-deps` keeps its lighter report and optional per-tier fixes for repository users;
`just check-deps --setup` verifies all developer requirements without installing them.
Live VM provisioning, guest images, and host worker services remain separate operator steps.
Choose a [run mode](#run-modes) below to configure backends and start the processes.

Test recipes use `uv`; hooks manage their Python/Go environments. Ansible regression tests
may inspect `/usr/bin/python3` to check host provisioning behavior, but do not require system
`pip`. See [ADR-0694](../adr/0694-complete-developer-setup.md).

The manifest recipe removes group-write permission from current-user-owned bootstrap files
and their ancestor directories, including checkout parents and external Python installations
(such as uv-managed Python), and the staging directory's ancestors. This supports a `0002`
umask without weakening runtime attestation. It logs each changed path, preserves sticky
shared directories, and does not change files owned by another user or remove world-write
permission. Remaining unsafe permissions still fail the build. For an intentionally
group-writable shared checkout, use a private checkout and interpreter instead. Direct manifest
`build` calls only change these permissions when passed `--prepare-permissions`; `verify`
and runtime verification never repair permissions.

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
Fedora, or `zypper install kmod` on SUSE. Resolution is **not** `PATH`-based: `depmod` is looked for in `/usr/sbin`,
`/usr/bin`, `/sbin`, and `/bin` only, so a copy installed elsewhere will not be found.
The service `doctor` (`kdivectl doctor --json`) carries a `depmod_toolchain` check when
local-libvirt is enabled. Supported container deployments disable local-libvirt under ADR-0088,
so they do not report this local worker-host check. If local-libvirt is enabled, install `kmod`
on that worker host as described above.

### Development and CI toolchain

Running the code from source, and reproducing the `just ci` gate, needs a build
toolchain in addition to the runtime backends. `libvirt-python` has no prebuilt wheels
and compiles against the system libvirt **and Python** headers, so those headers must be
present before `uv sync`. `just check-deps` reports gaps and may offer remediation when run
interactively; inspect its proposed actions before accepting them. Developers should run
`just setup` to install these dependencies and the remaining developer tools automatically.
The package examples below are for manual preparation.

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

**SLES / openSUSE:**

```bash
sudo zypper install gcc make pkg-config libvirt-devel python3-devel \
  libelf-devel ShellCheck shfmt git curl
```

On POWER, complete the [architecture-specific prerequisites](
../development/cross-platform.md#ppc64le-only), including Rust, before installing `just`/`prek`
or syncing the environment. Then install [uv](https://docs.astral.sh/uv/) and the CLIs:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install rust-just
just setup
```

#### Bash and GNU tools (all hosts; Homebrew on macOS)

Developer scripts need Bash 4.4 or later as the first `bash` on `PATH`, and the GNU
versions of coreutils, findutils, and grep ([ADR-0673](../adr/0673-developer-host-bash-and-gnu-tools-floor.md)).
Linux distributions supply these. `just check-deps` stops when Bash is older than 4.4, and
it reports a missing GNU tool in its Recommended tier.

macOS ships Bash 3.2 and BSD tools. Install the Homebrew versions:

```bash
brew install bash coreutils findutils grep
```

Then put them ahead of the system directories. Add these lines to `~/.zprofile`, so login
shells, terminals, and the git hooks they start get them:

```bash
eval "$(/opt/homebrew/bin/brew shellenv)"   # /usr/local/bin/brew on Intel Macs
for formula in coreutils findutils grep; do
  PATH="$(brew --prefix)/opt/$formula/libexec/gnubin:$PATH"
done
export PATH
```

Open a new terminal and check that `bash --version` shows 4.4 or later and
`realpath --version` shows GNU coreutils. GUI git clients do not read `~/.zprofile`, so a push
from one runs the pre-push hook with the system `bash`. Push from a terminal. The `gnubin`
directories hide the BSD `stat`, `find`, `grep`, and other tools in every shell that reads
this file.

The full `just ci` gate additionally exercises Docker (disposable Postgres/SeaweedFS via
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

See [remote-libvirt](providers/remote-libvirt.md) for remote-provider requirements.

### Local-libvirt host preparation

From a complete KDIVE checkout, prepare a local-libvirt host with the canonical Ansible-backed
recipe. It passes `--ask-become-pass`, so it requires an interactive become password and cannot run
unattended: the play installs distribution packages and writes system units as root, and this
repository holds no sudo credential to supply on the operator's behalf. On a host whose sudo
policy needs no password, an empty line on standard input answers the prompt; the
[host-installation producer](../development/coverage-qualification.md#host-installation-producer)
runs the recipe that way. It reads the lifecycle-witness database URL from the environment, so
the URL is not a process argument:

```bash
export KDIVE_LIFECYCLE_WITNESS_DATABASE_URL='<witness database URL>'
just prepare-local-libvirt-host
```

Before running the play, the recipe checks that the Ansible collections its roles need
(`deploy/ansible/requirements.yml`) can be resolved from `~/.ansible/collections` — the same tree
`just install-ansible-collections` installs to. Ansible resolves every module in an imported task
file at parse time, so a missing collection would otherwise break the play immediately, even for a
task guarded to run on a different host family. If a collection is missing, the recipe fails fast
and names `just install-ansible-collections` to run first.

The recipe installs and configures the local virtualization stack, worker lifecycle, project venv,
guestfs binding, and — on Debian and Ubuntu, which ship them `root:root 0600` — the `/boot` kernel
modes that guest-image builds need. It also installs an `/etc/kernel/postinst.d` hook that
re-applies those modes to a kernel a later upgrade installs, so no re-run is needed after one
(ADR-0668). Start a new login session after it completes so group
membership takes effect, then run `just check-local-libvirt`. Do not use `examples/local-libvirt/install-host.sh` as a
second installer; it only calls this recipe for compatibility with the example walkthrough.

The play's external-boot recovery capacity gate reserves
`live_vm_host_external_boot_concurrent_activations` (3 by default here, 96 GiB at 32 GiB each)
free bytes per worker slot, sized for a typical developer or lab host rather than the
self-hosted CI runner's own override. The gate's failure message names no byte counts; compute
the requirement yourself as 32 GiB times the knob's value, compared against
`df -B1 --output=avail` on each worker's recovery root
(`/var/lib/kdive/live-workers/external-boot-recovery/<worker>`). A host with less free space than
that needs a lower value, passed as an extra `-e` on the full recipe invocation (the `just`
recipe itself does not forward extra arguments):

```bash
export KDIVE_LIFECYCLE_WITNESS_DATABASE_URL='<witness database URL>'
ANSIBLE_CONFIG=deploy/ansible/ansible.cfg uv run --with 'ansible-core==2.21.1' \
  ansible-playbook deploy/ansible/playbooks/local-libvirt-host.yml --ask-become-pass \
  -e "local_libvirt_host_operator_user=${USER:?set USER to the operator account}" \
  -e live_vm_host_external_boot_concurrent_activations=2
```

| Family | Host-preparation status | Limits and proof strength |
|---|---|---|
| Debian / Ubuntu | Admitted, subject to the Python 3.14 guestfs binding check | Ubuntu 26.04 has a matching system binding. Other releases need the same check; no separate live apply is recorded here. |
| Fedora | Admitted, subject to the Python 3.14 guestfs binding check | Fedora 44 has a matching system binding; no live apply is claimed here. |
| RHEL / Rocky / AlmaLinux | EL10 admitted with matching-source binding build; other releases need an import check | EL10 packages Python 3.14 in AppStream but `python3-libguestfs` for system Python 3.12. Enable CRB/CodeReady Builder and a distribution source repository before host preparation. The role builds the 3.14 binding from the signed source RPM matching installed `libguestfs`, outside the recreated worker venv; the installer then verifies the linked import. A build failure stops preparation. See [local-libvirt](providers/local-libvirt.md#family-differences-that-matter) and the [support table](platform-support.md#host-and-guest-distributions) for live proof status. |
| SLES / openSUSE Tumbleweed | Admitted, subject to the Python 3.14 guestfs binding check | Structurally checked; no live apply is claimed. openSUSE Leap is a cataloged guest, not an admitted worker host. |

For foreign-architecture emulator packages and their availability, use the
[per-distro emulator table](platform-support.md#cross-architecture-guests); package names are
intentionally not duplicated here.
For each distro's separate host and guest status, see the
[host and guest distribution table](platform-support.md#host-and-guest-distributions).

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
