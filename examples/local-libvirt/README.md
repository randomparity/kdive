# Example: local-libvirt developer setup

A reference for the primary local-libvirt use case — a developer standing up KDIVE on
their own workstation and driving a kernel through its build → boot → debug → capture
lifecycle from an MCP client.

The server and reconciler run as you, the workers run in fixed slot accounts under the
root-owned lifecycle witness (ADR-0574; every worker incarnation is registered and credentialed
by it), and all of them share one operator-owned **session libvirt daemon** the contract
publishes. The stack onboards a project named **`demo`**, and an MCP client opened in your
kernel tree (`~/src/linux` by default) drives that very checkout.

This is the supported first-run path for a local-libvirt install. The scripts wrap the
maintained [live-stack lifecycle](../../docs/operating/runbooks/live-stack.md), adding host
preparation, project funding, guest-image registration, and MCP client configuration. Follow
[this page's usage sequence](#usage) for first setup; use the live-stack runbook for service
operation and diagnostics.

## Prerequisites

On a fresh Debian/Ubuntu or RedHat-family host, `install-host.sh` does all of the host
preparation below (see [Preparing a fresh host](#preparing-a-fresh-host)). The
[local-libvirt provider page](../../docs/operating/providers/local-libvirt.md) owns which
families are supported and how they differ. Otherwise:

- A KVM host with `libvirt` and a running `libvirtd`/`virtqemud`, the `default` network
  active, and your user in the `libvirt` group.
- Docker with a reachable daemon (for the Postgres / MinIO / mock-OIDC backends).
- The repo synced (`uv sync --locked`) so `.venv/bin/python` can `import kdive`. There is no
  PyPI wheel yet; the checkout is the install, and the scripts here run from it.
- The fixed live-worker lifecycle contract installed for this checkout and your user
  (`deploy/systemd/install-live-worker-lifecycle.sh`, root; `install-host.sh` runs it). A
  worker cannot start outside it — it has no incarnation credential — and it is what publishes
  the session libvirt endpoint every script here uses.
- A kdive-ready guest image at `/var/lib/kdive/rootfs/local/<name>.qcow2`, declared in
  `systems.toml` so an agent can provision it by catalog name. `build-image.sh` builds and
  registers one from the rootfs catalog (Fedora 44 is the kdump-capable default); it needs the
  backends up, so run it after `up.sh`.
- A kernel source tree at `KDIVE_KERNEL_SRC` (default `~/src/linux`).
- Local kdump capture requires drgn/libguestfs in the installed lifecycle worker environment,
  `/opt/kdive-live-worker-lifecycle/.venv`. The lifecycle installer/host role owns that environment.
  The checkout preflight probes `KDIVE_PYTHON`; it does not certify the installed worker's imports.

`up.sh` runs the preflight first and stops with an actionable message if anything is
missing. The kdump-only `guestfs`/`drgn` check is the one exception: `up.sh` runs the preflight
with `KDIVE_PREFLIGHT_KDUMP=optional`, so that gap prints as a `WARN` with the fix and the
bring-up continues — provision, build, boot, debug, and the other capture methods do not need
it. Export `KDIVE_PREFLIGHT_KDUMP=required` to make `up.sh` insist on it.

> **Day-to-day development?** `scripts/live-stack/up.sh` / `down.sh` / `status.sh` are the
> underlying host-lifecycle scripts; `up.sh` here adds the preflight, the project funding, and
> the `.mcp.json` merge on top of them, and is idempotent, so re-running it is the normal way to
> restart the stack after editing source (the daemons do not hot-reload).

## Files

| File | Purpose |
|------|---------|
| `install-host.sh` | Fresh Debian/Ubuntu or RedHat-family host preparation: host packages, `libvirt`/`kvm`/`docker` groups, readable host kernels, `uv` + `uv sync --group live`, the fixed live-worker lifecycle contract (root), the guest-image directory, the venv libguestfs binding. Re-runnable. |
| `env.sh` | Sources the live-stack env, then sets `KDIVE_PROJECT`, `KDIVE_GUEST_IMAGE`, `KDIVE_PYTHON`, the published session `KDIVE_LIBVIRT_URI`, and an XDG log directory. Source it; don't run it. |
| `up.sh` | Idempotent bring-up: control-group and endpoint check → preflight → `scripts/live-stack/up.sh` (backends, migrate, role bootstrap, session libvirt, daemons, lifecycle workers, inventory reconcile) → `scripts/live-stack/onboard.sh` (fund `demo`, verify, mint a token) → merge `.mcp.json`. |
| `build-image.sh` | Build one or more catalog images with `build-fs`, label the rootfs directory `svirt_image_t` on SELinux hosts (ADR-0639), append a `staged-path` `[[image]]` block to `systems.toml` from the build's provenance sidecar, and `reconcile-systems`. |
| `down.sh` | `scripts/live-stack/down.sh` with the example env: retires the lifecycle workers through the witness, stops the daemons and the compose backends, keeps state. `--wipe` also drops the data volumes and reaps kdive domains. |
| `mint-token.sh` | Print an admin developer token for `KDIVE_PROJECT` to stdout. |
| `mcp.json` | The MCP client config installed into the kernel tree; reads the token from `${KDIVE_TOKEN}` (holds no secret). |

## Usage

```bash
KDIVE_CHECKOUT="$PWD"    # run this block from the KDIVE checkout

# 0. Fresh Debian/Ubuntu host only: prepare it, then log out and back in for the groups.
examples/local-libvirt/install-host.sh

# 1. Bring everything up (no sudo: the lifecycle contract from step 0 does the privileged part).
examples/local-libvirt/up.sh

# 2. Build and register a guest image (once; re-run per extra distro you want to boot).
examples/local-libvirt/build-image.sh fedora-kdive-ready-44

# 3. In the shell you launch your MCP client from, export a fresh token:
export KDIVE_TOKEN=$(examples/local-libvirt/mint-token.sh)

# 4. Open your MCP client in the kernel tree — it reads the installed .mcp.json:
cd ~/src/linux            # the .mcp.json up.sh installed lives here
# ...launch your MCP client (it connects to http://127.0.0.1:8000/mcp as Bearer $KDIVE_TOKEN)

# 5. When finished, return to KDIVE and stop the stack (data is kept):
cd "$KDIVE_CHECKOUT"
examples/local-libvirt/down.sh
examples/local-libvirt/down.sh --wipe   # ...or also drop the database, the bucket, and kdive domains
```

## Preparing a fresh host

`install-host.sh` prepares Debian/Ubuntu (apt) and RedHat-family (dnf) hosts; it refuses anything
else with `exit 2` rather than installing a partial set. Validated targets: Ubuntu 26.04 and
Fedora 44. What it does, and why, so you can audit or redo a step:

- **Packages** — the operator set: libvirt + this host's QEMU emulator, libguestfs and its
  Python binding, `passt`, a container engine + compose, and the kernel build toolchain for the
  tree you will build and upload. The two families name these differently and the emulator is
  chosen differently on each (ADR-0637); the
  [provider page](../../docs/operating/providers/local-libvirt.md#family-differences-that-matter)
  lists every divergence. Debian/Ubuntu install through `scripts/apt-install.sh` (bounded retry);
  the RedHat family uses `dnf` directly, and Enterprise Linux gets CodeReady Builder enabled
  first for `libvirt-devel`.
- **Groups** — `libvirt`, `kvm`, `docker` for the invoking user, skipping any the host does not
  have (an Enterprise Linux host that satisfied the engine requirement with podman has no
  `docker` group). They apply on the next login shell, so the script ends by telling you to log
  out and back in rather than running the preflight (which would report the missing group).
- **Host kernels** — the libguestfs appliance that `build-fs` and the kdump harvest use reads a
  `/boot/vmlinuz-*`. Ubuntu ships those `root:0600`, so the script sets them to `root:kvm 0640`,
  the same posture as the CI runner's Ansible role; a kernel upgrade lands a new `0600` file, so
  re-run the script afterwards. Fedora already ships them `0755`, and the script leaves any
  kernel a non-owner can already read alone rather than narrowing it.
- **`uv sync --group live`** — the venv, plus `drgn` for the kdump capture path. Ubuntu 26.04
  and Fedora 44 both ship system Python 3.14, the same minor as the project's, so the script
  symlinks the distro libguestfs binding into the venv and the preflight's `import guestfs, drgn`
  check passes. On a host whose system Python differs — EL9 is 3.9, EL10 is 3.12 — it stays a
  `WARN` (kdump only) and everything else works. Contributors who also want the dev tooling (shellcheck, prek)
  run `./scripts/check-setup-deps.sh -y` separately.
- **Lifecycle contract** — `deploy/systemd/install-live-worker-lifecycle.sh --operator $USER
  --source <checkout>` as root, with the witness-member DSN on its standard input (the fixed
  local development login the compose `role-bootstrap` one-shot creates). It provisions the
  eight `kdive-worker-N` slot accounts, the `kdive-live-control`/`kdive-live-libvirt` groups
  (you join both), the root lifecycle witness socket, an operator-owned session `libvirtd` with
  its endpoint published in `/etc/kdive/live-worker-libvirt.env`, the provider data
  directories under `/var/lib/kdive`, and a worker venv under `/opt/kdive-live-worker-lifecycle`
  built from the checkout (re-run the script after pulling a new revision). The
  [live-stack runbook](../../docs/operating/runbooks/live-stack.md#prerequisites) and
  [`deploy/systemd/README.md`](../../deploy/systemd/README.md#fixed-live-worker-lifecycle-contract)
  describe the contract.
- **Directories** — `/var/lib/kdive/rootfs/local` (where `build-image.sh` publishes images) as
  you, group `kdive-live-libvirt`, the same posture the installer gives its parent.
- **SELinux labels** — on an enforcing host only, `svirt_image_t` on `/var/lib/kdive/rootfs` and
  `/var/lib/kdive/install`, the two trees a confined domain opens: provisioning writes each
  System's overlay and maps its baseline kernel/initrd under the first, and the install plane
  points a live domain's `<os>` at staged kernel/initrd under the second. `svirt_t` can do neither
  against the older `virt_image_t`, and the unprivileged session daemon performs no relabel of its
  own ([ADR-0639](../../docs/adr/0639-static-svirt-image-label-for-session-mode-domains.md)). The
  step no-ops off an enforcing host, and `build-image.sh` owns the nested `rootfs/local` rule.

Every catalog family builds through the customization boot (a throwaway guest installs its
own packages), so no image build depends on the libguestfs appliance network. The Ubuntu
24.04 `passt` failure (#694) applied to the earlier `virt-customize` path, retired in #1167.

## Tokens

`mint-token.sh` mints a bearer token from the mock-OIDC issuer carrying
`roles={KDIVE_PROJECT: admin}` plus `platform_admin`/`platform_operator`.

- **Lifetime.** The token expires after `KDIVE_TOKEN_TTL` seconds. The default is `2592000`
  (30d), set once in `scripts/live-stack/env.sh` and shared with `just onboard` — long enough
  that a multi-day build→boot→debug→capture cycle never hits mid-session expiry (the mock
  issuer's own default is one hour). The value must be a positive integer of seconds (minimum
  `1`); **no maximum is enforced** — `exp` is simply set to `now + KDIVE_TOKEN_TTL`, so you can
  mint a token that lasts as long as you like (a long-lived dev token is a mild security
  trade-off, acceptable only because the issuer is the bundled mock on your own machine). Set
  `KDIVE_TOKEN_TTL` before minting to change it.
- **Refreshing in a running session.** The installed `.mcp.json` carries
  `Authorization: Bearer ${KDIVE_TOKEN}`, and your MCP client expands `${KDIVE_TOKEN}` from
  its environment **once, when it connects** — it does not re-read the variable mid-session.
  So re-exporting `KDIVE_TOKEN` alone does nothing to a live connection. To pick up a new
  token (after expiry, or any time): re-run step 2 to export a fresh one, then **reconnect**
  the `kdive` server in your client (in Claude Code: `/mcp` → reconnect, or restart). Once a
  token expires, in-flight tool calls fail with `401` until you reconnect.

## How bring-up and teardown behave

- **Settle gate.** `scripts/live-stack/up.sh` waits past each daemon's own start budget and
  fails if the server or reconciler exits, or if the witness does not report exactly the
  requested worker slots (`KDIVE_WORKER_COUNT`, default 1) as started. Daemon logs are under
  `KDIVE_STACK_LOG_DIR`; worker diagnostics come from
  `scripts/live-stack/worker-lifecycle.sh diagnostics` (the units are retained by systemd).
- **Re-running replaces the fleet.** Bring-up asks the witness to retire any retained worker
  slots and stops its own daemons before starting again, so `up.sh` is the restart command;
  anything foreign holding the MCP port fails it loudly instead of losing the bind race.
- **Session libvirt, not `qemu:///system`.** The installer publishes one operator-owned session
  daemon; `env.sh` exports it as `KDIVE_LIBVIRT_URI`, so `build-fs`, the preflight, the daemons,
  and the workers all see the same domains. Files QEMU and virtlogd write belong to you, which
  is what lets a non-root worker confirm boots and read console logs (ADR-0223).
- **`.mcp.json` is merged, not clobbered.** If the file already exists its first version is
  backed up to `.mcp.json.bak` (never overwritten on re-run, so the original is preserved), and
  only the `kdive` server entry is replaced — any other MCP servers and top-level keys you
  configured are kept. A missing file is created from the template. The step is idempotent and
  runs last, so a missing kernel tree leaves a working stack behind.
- **Teardown goes through the witness.** `down.sh` retires the worker slots, stops the daemons
  (SIGTERM, then `--force` for SIGKILL), and stops the compose backends. Plain teardown keeps
  the data volumes and any running kdive domains; `--wipe` drops both.

## Configuration

Everything is overridable from the environment before running the scripts:

| Variable | Default | Meaning |
|----------|---------|---------|
| `KDIVE_PROJECT` | `demo` | Project the stack seeds and the token grants `admin` on. |
| `KDIVE_KERNEL_SRC` | `~/src/linux` | Kernel tree under test; where `.mcp.json` is installed. |
| `KDIVE_GUEST_IMAGE` | `…/fedora-kdive-ready-44.qcow2` | Local-disk rootfs the System boots, passed into the provision profile as `rootfs = {kind = "local", path = …}`. A file on disk, not an `image_catalog` object. |
| `KDIVE_LIBVIRT_URI` | the endpoint in `/etc/kdive/live-worker-libvirt.env` | libvirt connection every consumer drives — the operator-owned session daemon the lifecycle installer published. `qemu:///system` until the contract is installed, which `up.sh` refuses. |
| `KDIVE_PYTHON` | `<repo>/.venv/bin/python` | Interpreter for checkout commands, server, and reconciler; fixed workers use their installed lifecycle venv. |
| `KDIVE_LIMIT_KCU` / `KDIVE_MAX_ALLOC` / `KDIVE_MAX_SYS` | `1000000` / `4` / `4` | Seeded budget and quota. |
| `KDIVE_TOKEN_TTL` | `2592000` (30d) | Lifetime in seconds of the token `mint-token.sh` issues; inherited from `scripts/live-stack/env.sh`. Minimum `1`; no enforced maximum. |
| `KDIVE_STACK_LOG_DIR` | `~/.local/state/kdive/local-stack-logs` | Where the server and reconciler daemons log (workers log to their systemd units). |
| `KDIVE_WORKER_COUNT` | `1` | Lifecycle worker slots to start (1–8); workers are the job-concurrency unit. |
| `KDIVE_SKIP_OBS` | `1` | `0` also brings up Prometheus/Grafana with the backends. |
| `KDIVE_BUILD_IMAGE_WORKSPACE` | `~/.local/share/kdive/build/images` | User-writable `build-fs --workspace` for `build-image.sh` (the build-fs default under `/var/lib/kdive/build` is root-owned). |
| `KDIVE_LOCAL_ROLE_BOOTSTRAP` | `1` | `up.sh` runs the compose `role-bootstrap` one-shot so the per-process database login members exist; `0` skips it for externally provisioned members. |
| `KDIVE_SYSTEMS_TOML` | `~/.config/kdive/systems.toml` | Optional declarative inventory the reconciler loads. Absent by default (a quiet no-op) — see [Optional inventory](#optional-inventory-systemstoml). The default is CWD-independent; set this to point at a file elsewhere. |

The daemon logs live under the XDG state dir (`$XDG_STATE_HOME`, default
`~/.local/state/kdive`) — the same place the `kdive login` token cache lives — not inside
the repo. Set `XDG_STATE_HOME` or `KDIVE_STACK_LOG_DIR` to relocate them.

If you change `KDIVE_HTTP_HOST`/`KDIVE_HTTP_PORT` from `127.0.0.1:8000`, edit the `url` in
the installed `~/src/linux/.mcp.json` to match.

## Optional inventory (`systems.toml`)

This example needs **no** inventory file: host discovery alone makes your local libvirt
host allocatable, and `KDIVE_GUEST_IMAGE` is enough to boot a System. By default
`KDIVE_SYSTEMS_TOML` is unset, so the reconciler looks for
`~/.config/kdive/systems.toml`; when that file is absent it is a quiet no-op. The path is
resolved independently of the working directory — there is no repo-relative
`./systems.toml` fallback — so the stack behaves the same no matter where you launch it.

For an MCP agent with **no host shell**, `KDIVE_GUEST_IMAGE` is not enough on its own: that
host path is not visible from the MCP surface, so an agent cannot discover what to provision
with. Declaring a `staged-path` `[[image]]` (below) registers that local file in the catalog
so `images.list` / `systems.profile_examples` surface it and the agent provisions with a
`catalog` reference — no host `ls` (ADR-0228).

`build-image.sh` derives image architecture and capabilities from the qcow2's provenance
sidecar. When that sidecar contains an architecture-matched inspected root specification,
inventory reconciliation also adopts its digest without hashing the image; provisioning verifies
the actual bytes before use ([ADR-0624](../../docs/adr/0624-bind-staged-path-catalog-to-inspected-root.md)).
A sidecarless path remains a declaration, validated when provisioned. For an exact image binding
using a `local` rootfs reference, supply its `sha256` digest; a matching reconciled identity can
bind the same root authority as the catalog form. Reconciliation does not replace provenance on
an existing System.

For guest CPU pins and instruction-set requirements, use the host's advertised capabilities
in [the resources reference](../../docs/guide/reference/resources.md) and the provisioning
profile contract in [the systems reference](../../docs/guide/reference/systems.md). The
[platform guide](../../docs/operating/platform-support.md) covers native KVM and foreign-arch TCG.
Optional filesystem fixture overrides are covered by
[installation](../../docs/operating/install.md#optional-fixture-catalog-override).

If you want to declaratively pin host configuration, images, or prices, create
`~/.config/kdive/systems.toml`. The file must start with `schema_version = 2`. The
sections relevant to this **local-libvirt** example are below; the repo-root
[`systems.toml.example`](../../systems.toml.example) is the full annotated reference.

```toml
schema_version = 2

# Optional overlay onto the discovered local host. Discovery already registers the host
# and probes its size, so this block is optional — it only overlays config (name,
# cost_class, optional pool / concurrent_allocation_cap). It never overrides the
# discovered vcpus / memory_mb / PCIe fields.
[[local_libvirt]]
name = "workstation"
host_uri = "qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/libvirt-sock"  # the published endpoint
cost_class = "local"
# concurrent_allocation_cap = 1   # optional; how many allocations this host serves at once
# pool = "default"                # optional; group interchangeable hosts for by-pool allocation

# RECOMMENDED: register the local-disk rootfs `build-fs` wrote as a catalog image, so an
# MCP agent can DISCOVER it (`images.list` / `systems.profile_examples`) and provision with
# `rootfs = {kind = "catalog", provider = "local-libvirt", name = "fedora-kdive-ready-44"}` —
# no host `ls` and no `KDIVE_GUEST_IMAGE` needed. A `staged-path` source (ADR-0228) points at
# the host file directly: it seeds `registered` (bootable) immediately, with no object-store
# upload. The path must live under the provider `allowed_roots` (`/var/lib/kdive/rootfs`) and
# the image must be `public`. `source` is exactly one of s3 | build | staged | staged-path;
# use `s3` instead only if you publish the qcow2 to the object store (the row then stays
# `defined` and unbootable until the object exists).
#
# `fedora-kdive-ready-44` is the kdump-capable default (ADR-0251): its makedumpfile (1.7.9)
# filters current from-source kernels, so the default `kdump` `vmcore.fetch` captures a complete
# core.
[[image]]
provider = "local-libvirt"
name = "fedora-kdive-ready-44"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda"
visibility = "public"
capabilities = ["ssh", "selinux", "kdump", "drgn"]
[image.source]
kind = "staged-path"
path = "/var/lib/kdive/rootfs/local/fedora-kdive-ready-44.qcow2"

# `fedora-kdive-ready-43` is retained as the #817 regression reference (ADR-0251): its
# makedumpfile (1.7.8) cannot filter the newest kernels, so the default `kdump` method leaves an
# incomplete core on a from-source kernel — use 44 for that capture.
[[image]]
provider = "local-libvirt"
name = "fedora-kdive-ready-43"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda"
visibility = "public"
capabilities = ["ssh", "selinux", "kdump", "drgn"]
[image.source]
kind = "staged-path"
path = "/var/lib/kdive/rootfs/local/fedora-kdive-ready-43.qcow2"

# Price the `local` cost class (provider-agnostic). A host whose cost_class has no
# coefficient is admitted but denied every allocation (configuration_error).
[[cost_class]]
name = "local"
coeff = "1.0"
```

Place the file at the XDG default `~/.config/kdive/systems.toml` (or set
`KDIVE_SYSTEMS_TOML` to another path). With no `[[remote_libvirt]]` blocks, the stack
stays local-only.

There is no `[[build_config]]` section: ADR-0316 removed the server-build lane and with it the
kernel-config fragment machinery, so kdive never merges a `.config` for you. You build the kernel
yourself and upload it, and the `CONFIG_*` your investigation needs are advertised — not injected —
by `resource://kdive/contracts/external-build` (ADR-0318, ADR-0478). Start from
[the build-lane recipe](../../docs/operating/external-build-upload.md); if you intend to capture a
vmcore on a RHEL-family guest, read its kdump section before you build, because that set fails only
at capture time.

## Security notes

- The bundled mock-OIDC issuer mints a valid token for **any** caller. This example is for
  a developer's own machine only — never point `mint-token.sh` at a real deployment;
  production brings its own token via `$KDIVE_TOKEN`.
- `.mcp.json` references the token through `${KDIVE_TOKEN}` and never stores it, so the file
  is safe to leave in the kernel tree.
- `seed-project` writes budget/quota with raw `INSERT`s and no audit row — a deliberate
  token-less bootstrap for a single-developer box. Onboarding someone else's tenant uses
  the audited admin tools instead; see
  [project onboarding](../../docs/operating/project-onboarding.md).
