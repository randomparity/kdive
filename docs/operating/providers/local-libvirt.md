# Local-libvirt provider

The local-libvirt provider runs QEMU/KVM guests on the worker host's own libvirt daemon. It is the
shortest path to a working stack — no TLS, no second machine — and it is the provider a contributor
uses to exercise a change against a real VM.

This page owns which host families are supported and what each one needs. The scripts that carry
out the procedure live in [`examples/local-libvirt/`](../../../examples/local-libvirt/README.md),
which owns the command sequence, the guest-image build, and the MCP client wiring.

## Supported host families

`examples/local-libvirt/install-host.sh` prepares a host end to end. It classifies the host from
`/etc/os-release` and refuses anything outside these families rather than installing a partial set.

| Family | `install-host.sh` | Package names verified on |
|---|---|---|
| Debian / Ubuntu | full | Ubuntu 26.04 |
| Fedora | full | Fedora 44 |
| RHEL / CentOS Stream / Rocky / Alma | full, after you install a container engine | Rocky 9, Rocky 10 |
| Anything else (Arch, SUSE, …) | refuses with `exit 2` | — |

An unsupported host is not a dead end: the [prerequisites](#what-a-host-needs) below are the whole
contract, and a host that meets them by hand works with every later step. `deploy/ansible/roles/libvirt_stack`
covers the same package set for a fleet, and [#2388](https://github.com/randomparity/kdive/issues/2388)
tracks aiming it at the operator's own host.

## Setup path

1. Prepare the host: `examples/local-libvirt/install-host.sh`, then log out and back in so the new
   group memberships apply.
2. Bring the stack up: `examples/local-libvirt/up.sh` (idempotent; runs the preflight first).
3. Build and register a guest image: `examples/local-libvirt/build-image.sh fedora-kdive-ready-44`.
4. Mint a token and point an MCP client at the endpoint — see the
   [example walkthrough](../../../examples/local-libvirt/README.md#usage).
5. [Onboard a project](../project-onboarding.md) with a budget and quota for anything beyond the
   `demo` project `up.sh` funds.

The [live-stack runbook](../runbooks/live-stack.md) owns service operation and diagnostics once the
stack is up. The [configuration reference](../../guide/reference/config.md) owns runtime settings.
Kernel compilation happens outside KDIVE — follow the
[external-build upload guide](../external-build-upload.md).

## What a host needs

- **KVM and libvirt:** a running `libvirtd` (Debian/Ubuntu) or `virtqemud` (RedHat family), the
  `default` network active, and the operator in the `libvirt` and `kvm` groups.
- **A container engine:** the Postgres, MinIO, and mock-OIDC backends run under `docker compose`.
- **libguestfs and its Python binding:** `build-fs` and the kdump capture path build a supermin
  appliance, which needs a readable host kernel under `/boot`.
- **The checkout, synced:** there is no PyPI wheel yet — `uv sync --locked` in the checkout is the
  install.
- **The fixed live-worker lifecycle contract:** `deploy/systemd/install-live-worker-lifecycle.sh`
  installs the worker slot accounts, the root lifecycle witness, and the operator-owned session
  libvirt daemon. A worker cannot start outside it, so this is host preparation, not bring-up.

## Family differences that matter

These are the points where the two families genuinely diverge, not just in package naming.

- **Emulator package.** Debian/Ubuntu name the emulator by architecture (`qemu-system-x86`,
  `qemu-system-ppc`). The RedHat family answers by *nativeness* instead: `qemu-kvm` is the
  metapackage that pulls this host's own emulator, and Enterprise Linux ships no `qemu-system-*`
  package at all ([ADR-0637](../../adr/0637-redhat-qemu-emulator-package-by-nativeness.md)).
- **CodeReady Builder.** Enterprise Linux keeps `libvirt-devel` in CRB, disabled by default;
  `install-host.sh` enables it. Fedora has no CRB and needs nothing here.
- **Container engine.** Debian/Ubuntu package `docker.io` and Fedora packages `moby-engine`, both
  with a compose v2 binary. Enterprise Linux packages neither in baseos, appstream, extras, or CRB,
  so `install-host.sh` stops before changing the host and names the two real remedies: Docker's own
  repository, or `podman` with `podman-docker` and the podman socket API.
- **Host kernel permissions.** Debian/Ubuntu ship `/boot/vmlinuz-*` as `root:root 0600`, which the
  libguestfs appliance cannot read as a non-root user, so `install-host.sh` relabels them
  `root:kvm 0640` ([ADR-0222](../../adr/0222-ubuntu-build-fs-libguestfs-diagnostics.md)). Fedora
  ships them `0755` and is left alone. A Debian/Ubuntu kernel upgrade installs a fresh `0600` file:
  re-run the script afterwards.
- **SELinux.** Fedora and Enterprise Linux run SELinux enforcing, so `build-image.sh` labels the
  rootfs directory `virt_image_t` for the qemu user. `install-host.sh` installs
  `policycoreutils-python-utils` for the `semanage` that needs.
- **kdump capture on Enterprise Linux.** The libguestfs Python binding is a C extension built for
  the system Python, so it is importable from the project venv only when the two minor versions
  match. Ubuntu 26.04 and Fedora 44 both ship 3.14, the project Python; EL9 ships 3.9 and EL10
  ships 3.12, so neither can share the binding. Everything except kdump capture works there —
  provision, build, install, boot, debug, and the other capture methods do not use it.

## Preflight

From the checkout, `just check-local-libvirt` reports what is missing before the stack starts.
`up.sh` runs it first and stops with an actionable message, with one exception: the kdump-only
`guestfs`/`drgn` import check is downgraded to a `WARN` so bring-up continues without it. Export
`KDIVE_PREFLIGHT_KDUMP=required` to make that check blocking.

See [platform support](../platform-support.md) for supported guest architectures and the
per-distro customize-boot tiers, and [install](../install.md) for the deployment shapes and the
object-store requirements shared by every provider.
