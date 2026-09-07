# Example: local-libvirt developer setup

A reference for the primary local-libvirt use case — a developer standing up KDIVE on
their own workstation and driving a kernel through its build → boot → debug → capture
lifecycle from an MCP client.

The three KDIVE processes run **as root against `qemu:///system`** (the representative
identity for managing system-scope QEMU/KVM domains, libguestfs, kexec, and the console
log), the stack onboards a project named **`demo`**, and an MCP client opened in your
kernel tree (`~/src/linux` by default) drives that very checkout.

This is the supported first-run path for a local-libvirt install. The
[local-libvirt walkthrough](../../docs/operating/providers/local-libvirt-walkthrough.md) is the
step-by-step reference for what `up.sh` does and for the host preparation it expects (packages,
directories, the guest image); this page is the operating manual for the scripts.

This example calls the real product commands (`docker compose`, `python -m kdive migrate`,
`python -m kdive seed-project`) rather than the source-tree `just` recipes, so it mirrors a
real deployment. For the underlying reference material see
[`docs/operating/local-stack.md`](../../docs/operating/local-stack.md), the
[local-libvirt walkthrough](../../docs/operating/providers/local-libvirt-walkthrough.md),
and the [four-method live run](../../docs/operating/runbooks/four-method-live-run.md).

## Prerequisites

On a fresh Debian/Ubuntu host, `install-host.sh` does all of the host preparation below (see
[Fresh Debian/Ubuntu host](#fresh-debianubuntu-host)). Otherwise:

- A KVM host with `libvirt` and a running `libvirtd`/`virtqemud`, the `default` network
  active, and your user in the `libvirt` group.
- Docker with a reachable daemon (for the Postgres / MinIO / mock-OIDC backends).
- The repo synced (`uv sync --locked`) so `.venv/bin/python` can `import kdive`. There is no
  PyPI wheel yet; the checkout is the install, and the scripts here run from it.
- A kdive-ready guest image at `/var/lib/kdive/rootfs/local/<name>.qcow2`, declared in
  `systems.toml` so an agent can provision it by catalog name. `build-image.sh` builds and
  registers one from the rootfs catalog (Fedora 44 is the kdump-capable default); it needs the
  backends up, so run it after `up.sh`.
- A kernel source tree at `KDIVE_KERNEL_SRC` (default `~/src/linux`).
- For the **kdump capture leg only**: the worker venv must `import guestfs, drgn`. The
  preflight (`scripts/operations/check-local-libvirt.sh`) detects the gap and prints the one-time fix;
  see the [four-method runbook §4b](../../docs/operating/runbooks/four-method-live-run.md#wire-the-worker-venv-drgn--libguestfs).

`up.sh` runs the preflight first and stops with an actionable message if anything is
missing. The kdump-only `guestfs`/`drgn` check is the one exception: `up.sh` runs the preflight
with `KDIVE_PREFLIGHT_KDUMP=optional`, so that gap prints as a `WARN` with the fix and the
bring-up continues — provision, build, boot, debug, and the other capture methods do not need
it. Export `KDIVE_PREFLIGHT_KDUMP=required` to make `up.sh` insist on it.

> **Day-to-day development?** If you already have the stack seeded and just want to start/stop
> the kdive processes, use `scripts/live-stack/up.sh` / `down.sh` / `status.sh` — the
> host-lifecycle scripts that manage the running processes without re-seeding. This example
> (`examples/local-libvirt/`) is the **first-run onboarding** path.

## Files

| File | Purpose |
|------|---------|
| `install-host.sh` | Fresh Debian/Ubuntu host preparation: apt packages, `libvirt`/`kvm`/`docker` groups, readable host kernels, `uv` + `uv sync --group live`, `/var/lib/kdive` directories, the venv libguestfs binding. Re-runnable. |
| `env.sh` | Sources the live-stack env, then sets `KDIVE_PROJECT`, `KDIVE_GUEST_IMAGE`, `KDIVE_LIBVIRT_URI=qemu:///system`, and `KDIVE_PYTHON`. Source it; don't run it. |
| `up.sh` | Idempotent bring-up: preflight → duplicate-trio guard → staging dir → backends → migrate → runtime-role bootstrap → seed project → merge `.mcp.json` → start the trio as root → block until all three report ready. |
| `build-image.sh` | Build one or more catalog images with `build-fs`, label the rootfs directory for `qemu:///system` on SELinux hosts, append a `staged-path` `[[image]]` block to `systems.toml` from the build's provenance sidecar, and `reconcile-systems`. |
| `down.sh` | Stop the root processes by pid file — verifies each pid is still a kdive process (`sudo kill`, then SIGKILL survivors); keeps the pid file if a kdive process refuses to die. Backends are left running. |
| `mint-token.sh` | Print an admin developer token for `KDIVE_PROJECT` to stdout. |
| `mcp.json` | The MCP client config installed into the kernel tree; reads the token from `${KDIVE_TOKEN}` (holds no secret). |

## Usage

```bash
# 0. Fresh Debian/Ubuntu host only: prepare it, then log out and back in for the groups.
examples/local-libvirt/install-host.sh

# 1. Bring everything up (prompts once for sudo; starts root processes on qemu:///system).
examples/local-libvirt/up.sh

# 2. Build and register a guest image (once; re-run per extra distro you want to boot).
examples/local-libvirt/build-image.sh fedora-kdive-ready-44

# 3. In the shell you launch your MCP client from, export a fresh token:
export KDIVE_TOKEN=$(examples/local-libvirt/mint-token.sh)

# 4. Open your MCP client in the kernel tree — it reads the installed .mcp.json:
cd ~/src/linux            # the .mcp.json up.sh installed lives here
# ...launch your MCP client (it connects to http://127.0.0.1:8000/mcp as Bearer $KDIVE_TOKEN)

# 5. When finished, stop the processes (backends stay up):
examples/local-libvirt/down.sh
docker compose down       # from the repo root: stops the backends, keeps their data
docker compose down --volumes   # ...or also drop the database and the artifacts bucket
```

## Fresh Debian/Ubuntu host

`install-host.sh` is the scripted form of the walkthrough's Step 1 for apt-based hosts
(validated target: Ubuntu 26.04). What it does, and why, so you can audit or redo a step:

- **Packages** — the operator set: libvirt + the arch's QEMU emulator (`qemu-system-x86` or
  `qemu-system-ppc`; there is no `qemu-kvm` package on Ubuntu 26.04), libguestfs and its
  Python binding, `passt`, Docker + compose, and the kernel build toolchain for the tree you
  will build and upload. Installed through `scripts/apt-install.sh` (bounded retry).
- **Groups** — `libvirt`, `kvm`, `docker` for the invoking user. They apply on the next login
  shell, so the script ends by telling you to log out and back in rather than running the
  preflight (which would report the missing group).
- **Host kernels** — Ubuntu ships `/boot/vmlinuz-*` as `root:0600`; the libguestfs appliance
  that `build-fs` and the kdump harvest use reads one of them. The script sets them to
  `root:kvm 0640`, the same posture as the CI runner's Ansible role. A kernel upgrade lands a
  new `0600` file: re-run the script afterwards.
- **`uv sync --group live`** — the venv, plus `drgn` for the kdump capture path. Ubuntu 26.04's
  system Python is 3.14, the same minor as the project's, so `scripts/check-setup-deps.sh -y`
  can symlink the distro `python3-guestfs` binding into the venv and the preflight's
  `import guestfs, drgn` check passes; on a host whose system Python differs it stays a
  `WARN` (kdump only) and everything else works.
- **Directories** — `/var/lib/kdive/{install,console,rootfs/local,build}` owned by you,
  world-traversable.

Known gap on apt hosts: images in the **debian** family (`debian-kdive-ready-*`) are still
built with `virt-customize --install`, which needs the libguestfs appliance network (`passt`);
on Ubuntu 24.04 that failed (#694), and the fix is to move the family to the customization
boot the rhel family already uses (#1167). Fedora/Rocky/CentOS images (`rhel` family) do not
use the appliance network and build on an Ubuntu host.

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

- **Readiness gate.** `up.sh` does not print "stack is up" on a timer. After starting the
  trio it polls **all three** processes' `/readyz` and only prints success once every one
  returns `200` (the server gates on pg + MinIO + mock-OIDC; the worker and reconciler on
  pg + MinIO). If a process exits first or readiness is not reached within 90s, `up.sh`
  prints the per-process log paths under `KDIVE_STACK_LOG_DIR` and exits non-zero. `/readyz`
  is served by the aux health listener, **not** on the MCP port `:8000`, which has no health
  routes.
  - *Caveat:* with `KDIVE_HEALTH_BIND_ADDR` unset (the default), each process gets its own
    port — server `9464`, worker `9465`, reconciler `9466` — and `up.sh` polls all three. If
    you export `KDIVE_HEALTH_BIND_ADDR`, it applies to **all three** processes, so worker and
    reconciler collide with the server on that one port and fail to bind; `up.sh` then polls
    only that address and the per-process exit check surfaces the bind failure as a stopped
    process. Leave it unset.
- **Duplicate-run guard.** `up.sh` refuses to start if a root `kdive` trio is already
  running (a second trio would fight over the same domains); stop the first with `down.sh`.
- **`.mcp.json` is merged, not clobbered.** `up.sh` writes `KDIVE_KERNEL_SRC/.mcp.json`
  *before* starting the trio, so a problem here (missing kernel tree, malformed existing
  file) stops the run cleanly instead of leaving the trio up. If the file already exists its
  first version is backed up to `.mcp.json.bak` (never overwritten on re-run, so the original
  is preserved), and only the `kdive` server entry is replaced — any other MCP servers and
  top-level keys you configured are kept. A missing file is created from the template. The
  step is idempotent.
- **Teardown verifies process identity.** `down.sh` confirms each pid in the pid file is
  *still a `python -m kdive` process* before signalling it — a stale pid file may name a pid
  the OS has since recycled onto an unrelated (possibly root) process, and killing by bare
  number would take down the wrong thing. It sends `sudo kill`, waits, escalates to SIGKILL
  for kdive processes that remain, and reports both recycled pids (left untouched) and any
  kdive survivors. If a kdive process cannot be stopped it keeps the pid file and exits
  non-zero so a re-run can finish the job; a clean stop removes it.

## Configuration

Everything is overridable from the environment before running the scripts:

| Variable | Default | Meaning |
|----------|---------|---------|
| `KDIVE_PROJECT` | `demo` | Project the stack seeds and the token grants `admin` on. |
| `KDIVE_KERNEL_SRC` | `~/src/linux` | Kernel tree under test; where `.mcp.json` is installed. |
| `KDIVE_GUEST_IMAGE` | `…/fedora-kdive-ready-44.qcow2` | Local-disk rootfs the System boots, passed into the provision profile as `rootfs = {kind = "local", path = …}`. A file on disk, not an `image_catalog` object. |
| `KDIVE_LIBVIRT_URI` | `qemu:///system` | libvirt connection the worker drives. |
| `KDIVE_PYTHON` | `<repo>/.venv/bin/python` | Interpreter for `python -m kdive` and the processes. |
| `KDIVE_LIMIT_KCU` / `KDIVE_MAX_ALLOC` / `KDIVE_MAX_SYS` | `1000000` / `4` / `4` | Seeded budget and quota. |
| `KDIVE_TOKEN_TTL` | `2592000` (30d) | Lifetime in seconds of the token `mint-token.sh` issues; inherited from `scripts/live-stack/env.sh`. Minimum `1`; no enforced maximum. |
| `KDIVE_STACK_PID_FILE` / `KDIVE_STACK_LOG_DIR` | `~/.local/state/kdive/local-stack.pid` / `…/local-stack-logs` | Where `up.sh` records the process pids and writes per-process logs. |
| `KDIVE_BUILD_IMAGE_WORKSPACE` | `~/.local/share/kdive/build/images` | User-writable `build-fs --workspace` for `build-image.sh` (the build-fs default under `/var/lib/kdive/build` is root-owned). |
| `KDIVE_LOCAL_ROLE_BOOTSTRAP` | `1` | `up.sh` runs the compose `role-bootstrap` one-shot so the per-process database login members exist; `0` skips it for externally provisioned members. |
| `KDIVE_SYSTEMS_TOML` | `~/.config/kdive/systems.toml` | Optional declarative inventory the reconciler loads. Absent by default (a quiet no-op) — see [Optional inventory](#optional-inventory-systemstoml). The default is CWD-independent; set this to point at a file elsewhere. |

The pid file and logs live under the XDG state dir (`$XDG_STATE_HOME`, default
`~/.local/state/kdive`) — the same place the `kdive login` token cache lives — not inside
the repo. Set `XDG_STATE_HOME`, or the two `KDIVE_STACK_*` variables, to relocate them.

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

If you want to declaratively pin host config, prices, or build fragments, create
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
host_uri = "qemu:///system"
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
