# Local-libvirt provider

The local-libvirt provider runs QEMU/KVM guests on the worker host's own libvirt daemon. It is the
shortest path to a working stack — no TLS, no second machine — and it is the provider a contributor
uses to exercise a change against a real VM.

This page explains the local-libvirt host requirements. The
[host and guest distribution table](../platform-support.md#host-and-guest-distributions) owns
the per-distro status. The scripts that carry out the procedure live in
[`examples/local-libvirt/`](../../../examples/local-libvirt/README.md), which owns the command
sequence, the guest-image build, and the MCP client wiring.

## Host admission and installation

`examples/local-libvirt/install-host.sh` delegates to `just prepare-local-libvirt-host`, whose
Ansible role admits Debian, Ubuntu, Fedora, RHEL, Rocky, AlmaLinux, openSUSE Tumbleweed, and SLES.
Admission alone does not establish a usable worker: the lifecycle installer requires the Python
3.14 `guestfs` binding. EL10 packages its binding for system Python 3.12, so host preparation
builds one for Python 3.14 from the matching signed source RPM. CentOS Stream, openSUSE Leap,
and Arch are rejected by the worker role. See the
[host and guest distribution table](../platform-support.md#host-and-guest-distributions) for
the separate host-install and guest-image status of each distro.

The clean-host installation proof for #2807 ran this path from a fresh baseline through a real
guest, repeated setup, and booted again. On Fedora 44 every check held. Ubuntu 26.04 installed
and booted, but its session-mode guests run unconfined under AppArmor (#3067). Rocky Linux 10
stops at host preparation when Docker comes from Docker's repository (#3068). See the
[proof record](../../design/2026-10-01-host-install-proof-record-2807.md).

## Setup path

1. Prepare the host: `examples/local-libvirt/install-host.sh`, then log out and back in so the new
   group memberships apply.
2. Bring the stack up: `examples/local-libvirt/demo-up.sh` (idempotent; runs the preflight first).
3. Build and register a guest image: `examples/local-libvirt/build-image.sh fedora-kdive-ready-44`
   (`fedora-kdive-ready-44` is the x86_64 catalog entry; on a ppc64le host build
   `fedora-kdive-ready-44-ppc64le` instead — `env.sh`'s `KDIVE_GUEST_IMAGE` default follows
   `uname -m`).
4. Mint a token and point an MCP client at the endpoint — see the
   [example walkthrough](../../../examples/local-libvirt/README.md#usage).
5. [Onboard a project](../project-onboarding.md) with a budget and quota for anything beyond the
   `demo` project `demo-up.sh` funds.

The [live-stack runbook](../runbooks/live-stack.md) owns service operation and diagnostics once the
stack is up. The [configuration reference](../../guide/reference/config.md) owns runtime settings.
Kernel compilation happens outside KDIVE — follow the
[external-build upload guide](../external-build-upload.md).

## What a host needs

- **KVM and libvirt:** a running `libvirtd` (Debian/Ubuntu) or `virtqemud` (RedHat family), the
  `default` network active, and the operator in the `libvirt` and `kvm` groups.
- **A container engine:** the Postgres, SeaweedFS, and mock-OIDC backends run under `docker compose`.
- **libguestfs and its Python binding:** the lifecycle worker imports the `guestfs` binding to
  provision (baseline-kernel extraction, ADR-0272), stage built kernels, boot external kernels,
  run `build-fs`, and capture kdump locally. Each libguestfs launch builds a supermin appliance,
  which needs a readable host kernel under `/boot`.
- **Host libselinux:** the external-boot worker evaluates an inactive SELinux guest's file-context
  policy for the final module paths before it records the target identity. The local worker Ansible
  role installs the distribution's libselinux package; a missing library stops preparation.
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
  package at all ([ADR-0641](../../adr/0641-redhat-qemu-emulator-package-by-nativeness.md)).
- **CodeReady Builder and source repositories.** Enterprise Linux keeps `libvirt-devel` and
  `libguestfs-devel` in CRB/CodeReady Builder. Enable it before `install-host.sh`; on Rocky or
  AlmaLinux use `sudo dnf config-manager --set-enabled crb` after installing
  `dnf-plugins-core`. On subscribed RHEL, enable its CodeReady Builder repository through
  `subscription-manager`. The EL10 binding builder also needs the distribution's AppStream
  source repository definition; it enables source repositories only for its exact-source
  download. Fedora uses its packaged binding.
- **Container engine.** The engine and the compose v2 plugin are separate packages on every
  family: Debian/Ubuntu pair `docker.io` with `docker-compose-v2`, Fedora pairs `moby-engine`
  with `docker-compose`, and openSUSE Tumbleweed pairs `docker` with `docker-compose`. In each
  case the compose package is what provides the `docker compose` subcommand; the engine alone
  does not. Enterprise Linux packages no engine in baseos, appstream, extras, or CRB, and SLES
  ships Docker only in the Containers Module, so neither family gets a declared runtime and the
  two real remedies there are Docker's own repository, or `podman` with `podman-docker` and the
  podman socket API. `stack-services.sh` refuses to run as root, so the operator needs the
  socket, and the packages alone do not give it: the RPM leaves `docker.service` disabled and
  creates the `docker` group empty. `just prepare-local-libvirt-host` now enables and starts
  `docker.service` and adds the operator account to the `docker` group
  ([ADR-0663](../../adr/0663-provisioning-enables-the-container-engine-daemon.md)). It does that on
  any host carrying `/usr/lib/systemd/system/docker.service` — which on Debian/Ubuntu means an
  engine you installed yourself, since the standalone role declares one only on Fedora and openSUSE
  Tumbleweed — and that includes an Enterprise Linux or SLES host that took the Docker-repository
  remedy above — this repository still installs no engine
  there, it only makes one you installed usable. A `podman-docker` host has no such unit, so both
  steps skip and its socket path stays yours. The new group does not reach a login session that
  already existed, so start a fresh one before running `stack-services.sh`.
- **Host kernel permissions.** Debian/Ubuntu ship `/boot/vmlinuz-*` as `root:root 0600`, which the
  libguestfs appliance cannot read as a non-root user, so `just prepare-local-libvirt-host`
  relabels them `root:kvm 0640` and asserts that every fixed worker account is in `kvm`,
  which is what that mode grants read through
  ([ADR-0222](../../adr/0222-ubuntu-build-fs-libguestfs-diagnostics.md)). Fedora ships them
  world-readable and is left alone. A Debian/Ubuntu kernel upgrade installs a fresh `0600` file
  under a new name, so the recipe also installs an `/etc/kernel/postinst.d` hook that re-applies
  the mode at install time
  ([ADR-0668](../../adr/0668-a-kernel-upgrade-re-applies-the-boot-relabel.md)); no re-run is
  needed. `just check-deps` and `just check-local-libvirt` both report the unfixed state.
- **SELinux.** Fedora and Enterprise Linux run SELinux enforcing, so
  `just prepare-local-libvirt-host` and `build-image.sh` label the kdive image directories
  `svirt_image_t` for the confined domain (ADR-0640). The recipe installs
  `policycoreutils-python-utils` for the `semanage` that needs, labels `/var/lib/kdive/rootfs` and
  `/var/lib/kdive/install`, and `just check-local-libvirt` fails while either lacks the label.
  If a domain start still fails with `Permission denied` on a kdive image, a stale
  per-domain label may be stuck — `sudo restorecon -R -F /var/lib/kdive/rootfs` clears it.

  `build-image.sh` applies the same label to its resolved `KDIVE_BUILD_IMAGE_WORKSPACE` before its
  session-daemon customization boot. This covers the temporary disk and direct-kernel inputs that
  build-fs creates beneath that workspace, including when the configured path is a symlink. The
  workspace rule treats its path as a literal fcontext prefix, so an operator-selected name does
  not broaden the label beyond that directory and descendants.
- **libguestfs backend.** The worker pins `LIBGUESTFS_BACKEND=direct` in
  `deploy/systemd/system/kdive-live-worker@.service`. Debian/Ubuntu build libguestfs with that
  backend as its default; Fedora and RHEL default to the libvirt backend, which connects to
  `qemu:///session` and needs `$HOME/.cache/libvirt`. The fixed worker slot accounts have no home
  directory, so on a RedHat-family host the unpinned default fails the first provision with
  `Cannot create user runtime directory '/nonexistent/.cache/libvirt': Permission denied`,
  reported as an `infrastructure_failure`.
- **Core-file limit.** The lifecycle installer raises `RLIMIT_CORE` before launching the session
  libvirt daemon. Classic sudo zeroes it, so every RedHat-family host would otherwise reach the
  launch with a zero hard limit and fail every domain start with `cannot limit core file size of
  process N`; Ubuntu 26.04's sudo-rs does not zero it.
- **Which interpreter the host play runs under.** `playbooks/local-libvirt-host.yml` pins
  `ansible_python_interpreter: /usr/bin/python3` as a play var. It manages the host's libvirt stack
  through the distro-packaged `libvirt` and `lxml` bindings, and interpreter discovery would
  otherwise select whichever Python launched `ansible-playbook` — for `just
  prepare-local-libvirt-host` that is an ephemeral `uv run` environment with no `lxml`, which fails
  `community.libvirt` in `libvirt_pool_net`. The pin is a play var, and `inventory/hosts.yml`
  deliberately declares no `localhost`: that file is shared with `playbooks/pki.yml` and with the
  localhost plays under `deploy/ansible/tests/` that do not pin an interpreter of their own, all of
  which resolve their dependencies against the launching environment. Declaring the host there
  would swap their explicit launcher interpreter for ansible-core's own interpreter discovery, so
  the pin binds only this play. It also fixes the family bound: on Debian/Ubuntu, Fedora and the EL
  family, `/usr/bin/python3` is the interpreter `libvirt_stack` installs `python3-libvirt` and
  `python3-lxml` for; its Suse branch installs `python3-libvirt-python`, which zypper resolves
  against the distro's default Python ABI — the same interpreter. A `/usr/bin/python3` predating
  ansible-core's 3.9 target floor fails closed on Ansible's own error at fact gathering, and the
  recipe carries no pre-check. EL9's 3.9 sits exactly on that floor, so the first bump of the
  `ansible-core` pin in `just prepare-local-libvirt-host` that raises the floor retires EL9 here.
- **The interpreter, and what it costs Enterprise Linux.** The project requires Python 3.14.
  Ubuntu 26.04 and Fedora 44 ship it as `/usr/bin/python3`; EL9 ships 3.9 and EL10 ships 3.12,
  packaging 3.14 separately as `python3.14`, which `install-host.sh` installs and the lifecycle
  contract discovers. The consequence is the libguestfs Python binding: it is a C extension built
  for the *system* interpreter, so it loads in a venv only when the two minor versions match. On
  EL they cannot, and EL ships the binding only for its system Python. Without a binding in the
  lifecycle worker venv, provisioning fails with `libguestfs (the guestfs Python binding) is
  required to extract the baseline kernel`, and so do guest-image builds (`build-fs`), built-kernel
  staging, external boot and local kdump capture (ADR-0203). On EL10, `local_worker_host`
  builds the Python 3.14 extension from the signed distro source RPM whose version matches
  installed `libguestfs` and `libguestfs-devel`; it stores the result outside the worker venv.
  The lifecycle installer then links it from the venv's base interpreter and fails if import
  is still unavailable. Source repository or build failure stops host preparation.
  `just check-local-libvirt` probes the
  installed worker venv for the binding. Note also that an EL host cannot build a *btrfs* image even with a working binding: the EL libguestfs
  appliance kernel has no btrfs, so a Fedora cloud image fails with `unknown filesystem type
  'btrfs'`. The catalog's `rocky-kdive-ready-*` entries are the EL-native choice.

## Preflight

From the checkout, `just check-local-libvirt` reports what is missing before the stack starts.
`demo-up.sh` runs it first and stops with an actionable message, with one exception: the kdump-only
`guestfs`/`drgn` import check is downgraded to a `WARN` so bring-up continues without it. Export
`KDIVE_PREFLIGHT_KDUMP=required` to make that check blocking.

See [platform support](../platform-support.md) for supported guest architectures and the
per-distro customize-boot tiers, and [install](../install.md) for the deployment shapes and the
object-store requirements shared by every provider.
