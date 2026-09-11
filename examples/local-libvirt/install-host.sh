#!/usr/bin/env bash
# Prepare a fresh Debian/Ubuntu or RedHat-family host for the local-libvirt developer setup.
# Idempotent; safe to re-run. Installs the host packages, adds the invoking user to the
# libvirt/kvm/docker groups, makes the host kernels readable for the libguestfs appliance,
# installs uv, builds the project venv, installs the fixed live-worker lifecycle contract (root),
# and wires the kdump libguestfs binding.
#
#   examples/local-libvirt/install-host.sh
#
# Then log out and back in (the new group memberships take effect on a new login shell) and
# continue with examples/local-libvirt/up.sh. Supported host families and their per-family
# caveats are in docs/operating/providers/local-libvirt.md; any other host needs the
# prerequisites in docs/operating/install.md and the lifecycle setup described there.
set -euo pipefail

example_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${example_dir}/../.." && pwd)"

step() { printf '\n=== %s ===\n' "$1"; }

# 1. Distro and arch gates. Two host families are supported; the package names for each live in
#    step 2. Family tokens follow scripts/check-setup-deps.sh's load_distro_id so one host is
#    never classified two ways. `is_fedora` splits Fedora from Enterprise Linux inside the
#    RedHat family — they agree on every virtualization package name but differ on the container
#    engine and on whether the CRB repo has to be enabled (see steps 1b and 2).
os_release="${KDIVE_OS_RELEASE:-/etc/os-release}"
distro_id="" distro_like=""
if [[ -r "${os_release}" ]]; then
  # shellcheck disable=SC1090
  distro_id="$(. "${os_release}" && printf '%s' "${ID:-}")"
  # shellcheck disable=SC1090
  distro_like="$(. "${os_release}" && printf '%s' "${ID_LIKE:-}")"
fi
case " ${distro_id} ${distro_like} " in
*" debian "* | *" ubuntu "*) distro_family="debian" ;;
*" fedora "* | *" rhel "* | *" centos "*) distro_family="redhat" ;;
*)
  echo "install-host.sh covers Debian/Ubuntu (apt) and Fedora/RHEL-family (dnf) hosts;" >&2
  echo "this host reports ID=${distro_id:-?} ID_LIKE=${distro_like:-?}." >&2
  echo "See docs/operating/providers/local-libvirt.md for the supported families and the" >&2
  echo "manual prerequisites any other host needs." >&2
  exit 2
  ;;
esac
readonly distro_family
is_fedora=0
[[ "${distro_id}" == "fedora" ]] && is_fedora=1
readonly is_fedora

# The emulator package is arch-named on Debian/Ubuntu, but the RedHat family answers by
# NATIVENESS instead: `qemu-kvm` is the metapackage that pulls exactly this host's own emulator,
# and EL ships no qemu-system-* package at all (ADR-0637). scripts/check-setup-deps.sh keeps the
# same split in package_for.
host_arch="$(uname -m)"
case "${distro_family}:${host_arch}" in
debian:x86_64) qemu_package="qemu-system-x86" ;;
debian:ppc64le) qemu_package="qemu-system-ppc" ;;
redhat:x86_64 | redhat:ppc64le) qemu_package="qemu-kvm" ;;
*)
  echo "host arch ${host_arch} is not a supported kdive provisioning arch (x86_64, ppc64le)" >&2
  exit 2
  ;;
esac

if ((EUID == 0)); then
  echo "run this as the user who will operate kdive, not as root: it adds that user to the" >&2
  echo "libvirt/kvm/docker groups and builds the venv in their checkout (sudo is used inline)." >&2
  exit 2
fi

# 1b. Container engine. up.sh runs the Postgres / MinIO / mock-OIDC backends through
#     `docker compose`, so the host needs a docker-compatible engine. Debian/Ubuntu and Fedora
#     package one (docker.io / moby-engine, with the compose v2 plugin); Enterprise Linux
#     packages neither in baseos/appstream/extras/CRB — verified on Rocky 9 and Rocky 10 — so
#     there is no name this script could add to the EL set. Fail here with the two real remedies
#     rather than installing everything else and leaving up.sh to fail on the backends.
if [[ "${distro_family}" == "redhat" ]] && ((!is_fedora)) && ! command -v docker >/dev/null 2>&1; then
  echo "no 'docker' on PATH, and Enterprise Linux packages no container engine this script can" >&2
  echo "install (neither moby-engine nor docker-compose is in baseos/appstream/extras/CRB)." >&2
  echo "Install one, then re-run:" >&2
  echo "  - Docker's own repo: dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo" >&2
  echo "    then: dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin" >&2
  echo "  - or podman: dnf install -y podman podman-docker, then enable the podman.socket API" >&2
  echo "Fedora needs none of this; it packages moby-engine and docker-compose." >&2
  exit 2
fi

step "sudo (package install, group membership, /var/lib/kdive)"
# Prompt only when a password is needed: `sudo -v` insists on a terminal even where a NOPASSWD
# rule applies, which breaks a non-interactive run (ssh without a tty, nohup).
sudo -n true 2>/dev/null || sudo -v

# 2. Host packages: the operator set from the walkthrough (Step 1) plus the CI runner's
#    (elfutils/debuginfod for the debuginfo lane). The build toolchain is here because the
#    kernel under test is built on this host and uploaded on the build lane; the worker itself
#    never compiles kernel source (ADR-0316). On ppc64le, pydantic-core and the just/prek CLIs
#    build from source, so a Rust toolchain is needed as well (rustup; see the cross-platform
#    guide).
#
#    The two sets are the same dependencies under each family's names; keep them in step. Every
#    RedHat-family name below was resolved against live repositories on Fedora 44, Rocky 9, and
#    Rocky 10. Three differences are not name changes and are called out where they sit:
#    `build-essential` has no RedHat counterpart (gcc Requires glibc-devel there, and make and
#    pkg-config are listed on their own), the container engine is Fedora-only (step 1b), and
#    `policycoreutils-python-utils` is RedHat-only — build-image.sh needs `semanage` to label
#    the rootfs directory virt_image_t on an SELinux-enforcing host.
if [[ "${distro_family}" == "debian" ]]; then
  # `qemu-kvm` is deliberately absent: the arch emulator package provides KVM and the
  # transitional name no longer exists on Ubuntu 26.04.
  packages=(
    build-essential pkg-config libvirt-dev python3-dev
    libvirt-daemon-system libvirt-clients qemu-utils "${qemu_package}"
    libguestfs-tools python3-guestfs passt e2fsprogs elfutils debuginfod
    gcc make flex bison bc libssl-dev libelf-dev rsync xz-utils git curl ca-certificates
    docker.io docker-compose-v2 gdb
  )
else
  packages=(
    pkgconf-pkg-config libvirt-devel python3-devel
    libvirt libvirt-client qemu-img "${qemu_package}"
    guestfs-tools python3-libguestfs passt e2fsprogs elfutils elfutils-debuginfod-client
    gcc make flex bison bc openssl-devel elfutils-libelf-devel rsync xz git curl ca-certificates
    gdb policycoreutils-python-utils
  )
  # Fedora packages the engine and the compose v2 binary; EL reached step 1b instead.
  ((is_fedora)) && packages+=(moby-engine docker-compose)
fi

if [[ "${distro_family}" == "redhat" ]] && ((!is_fedora)); then
  # libvirt-devel is in CodeReady Builder, which Enterprise Linux disables by default (#2404):
  # without this the install fails with "No match for argument: libvirt-devel". Fedora has no
  # CRB. dnf4 (EL today) spells this `--set-enabled`; dnf5 dropped that for `setopt`, so try
  # both rather than pin the script to one dnf generation. config-manager is a plugin and is
  # absent from a minimal install, so install it first.
  step "CodeReady Builder repo (libvirt-devel)"
  sudo dnf install -y dnf-plugins-core
  sudo dnf config-manager --set-enabled crb ||
    sudo dnf config-manager setopt crb.enabled=1
fi

step "${distro_family} packages (${#packages[@]})"
if [[ "${distro_family}" == "debian" ]]; then
  # apt-install.sh refreshes the index itself, escalates through sudo, and bounds each call with
  # a timeout + retry (ADR-0566). The default per-call budget is sized for CI's small header
  # set; this set pulls the libguestfs appliance and Docker, so give it more room.
  KDIVE_APT_TIMEOUT_S="${KDIVE_APT_TIMEOUT_S:-600}" \
    "${repo_root}/scripts/apt-install.sh" "${packages[@]}"
else
  # No dnf counterpart to apt-install.sh: ADR-0566's wrapper exists for a specific observed
  # apt-get stall on CI runners (#1978), and dnf carries its own `timeout`/`retries` settings.
  # Add one if a dnf stall is ever observed, not before.
  sudo dnf install -y "${packages[@]}"
fi

# 2b. Start the libvirt daemons (RedHat family only). Installing the packages leaves the modular
#     socket units *enabled but not started*, so a host that has not rebooted since the install
#     has no libvirt listening: `virsh -c qemu:///system` fails and the `default` network stays
#     inactive, which is exactly where the preflight and up.sh stop. Debian/Ubuntu need nothing
#     here — libvirt-daemon-system's postinst starts libvirtd.socket itself, and that path is
#     already proven, so leave it alone. The unit list mirrors libvirt_stack_modular_sockets in
#     deploy/ansible/roles/libvirt_stack; starting virtnetworkd is what brings `default` up.
if [[ "${distro_family}" == "redhat" ]]; then
  step "modular libvirt sockets"
  sudo systemctl enable --now \
    virtqemud.socket virtnetworkd.socket virtstoraged.socket \
    virtnodedevd.socket virtsecretd.socket virtproxyd.socket
fi

# 3. Group membership. Takes effect on the next login shell, which is why the script ends with
#    a reminder instead of running the preflight (it would report the libvirt group as missing).
#    Only groups that exist are requested: `usermod -aG` fails the whole call on an unknown
#    group, and an EL host that satisfied step 1b with podman has no `docker` group.
step "groups for ${USER}"
host_groups=()
for group in libvirt kvm docker; do
  getent group "${group}" >/dev/null 2>&1 && host_groups+=("${group}")
done
if ((${#host_groups[@]} == 0)); then
  echo "none of libvirt/kvm/docker exist on this host; expected them from step 2" >&2
  exit 1
fi
printf '  %s\n' "${host_groups[*]}"
sudo usermod -aG "$(
  IFS=,
  printf '%s' "${host_groups[*]}"
)" "${USER}"

# 4. Host kernels. libguestfs builds its supermin appliance from one, so build-fs and the kdump
#    harvest fail for a non-root user until they are readable (ADR-0222, #694). Debian/Ubuntu
#    ship /boot/vmlinuz-* root:root 0600 and need the fix; Fedora ships them 0755 and does not.
#    Widening a kernel that is ALREADY readable would narrow it instead — 0640 root:kvm is
#    tighter than 0755 — so act only on the ones a non-owner cannot read. 0640 root:kvm keeps
#    the read scoped to the KVM group, the same posture deploy/ansible/roles/live_vm_host
#    applies. A Debian/Ubuntu kernel upgrade installs a new 0600 file: re-run this script (or
#    just this step) afterwards.
step "host kernels readable for the libguestfs appliance"
for kernel in /boot/vmlinuz-* /boot/vmlinux-*; do
  [[ -e "${kernel}" ]] || continue
  read -r kernel_mode kernel_group < <(stat -c '%a %G' "${kernel}")
  # Readable by others, or already group-readable by kvm: leave it alone.
  if ((kernel_mode % 10 >= 4)) ||
    { [[ "${kernel_group}" == "kvm" ]] && ((kernel_mode / 10 % 10 >= 4)); }; then
    echo "  ${kernel}: already readable (${kernel_mode} ${kernel_group}), unchanged"
    continue
  fi
  sudo chgrp kvm "${kernel}"
  sudo chmod 0640 "${kernel}"
  echo "  ${kernel}: root:kvm 0640"
done

# 5. uv, then the project venv. `--group live` adds drgn, which the kdump capture path imports
#    from the worker venv alongside the libguestfs binding wired in step 9.
step "uv"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
fi
uv --version

step "uv sync --group live (${repo_root})"
(cd "${repo_root}" && uv sync --group live)
"${repo_root}/.venv/bin/python" -m kdive --help >/dev/null

# 6. The fixed live-worker lifecycle contract (ADR-0555/ADR-0574): eight retained worker slot
#    accounts, the root socket-activated lifecycle witness that registers each worker
#    incarnation and hands it its credential, a dedicated operator-owned session libvirt
#    daemon, the provider data directories under /var/lib/kdive, and an installed worker venv
#    under /opt/kdive-live-worker-lifecycle. A plain `python -m kdive worker` cannot start
#    without it (no incarnation credential), so this is part of host preparation, not of the
#    stack bring-up. Root, idempotent for one checkout; the witness-member DSN goes in on
#    standard input only (never an argument). The member itself is created by the compose
#    role-bootstrap one-shot up.sh runs, with this fixed local development password.
step "fixed live-worker lifecycle contract (deploy/systemd/install-live-worker-lifecycle.sh)"
witness_password="kdive-witness-local" # pragma: allowlist secret — local development only
witness_dsn="postgresql://kdive-witness-member:${witness_password}@localhost:${KDIVE_POSTGRES_PORT:-5432}/kdive"
printf '%s\n' "${witness_dsn}" | sudo env "PATH=${PATH}" \
  "${repo_root}/deploy/systemd/install-live-worker-lifecycle.sh" \
  --operator "${USER}" --source "${repo_root}"
unset witness_dsn witness_password

# 7. Where build-image.sh publishes guest images: the installer made /var/lib/kdive/rootfs the
#    operator's, group kdive-live-libvirt, so the fixed workers can read every image and write
#    their per-System overlays beside them; `local/` follows the same posture.
step "/var/lib/kdive/rootfs/local"
sudo install -d -o "${USER}" -g kdive-live-libvirt -m 2770 /var/lib/kdive/rootfs/local

# 8. The fixed workers run in their own accounts and execute this checkout's source (through
#    scripts/live-stack/worker-from-checkout) and read the kernel tree, so every directory on
#    the way there must be traversable by them; the witness refuses to start a worker otherwise.
#    The Ansible role gets this by cloning under root-owned /opt; a developer checkout usually
#    sits under $HOME, which Ubuntu creates 0750 and Fedora 0700. Grant traverse (x) to others
#    on each ancestor that lacks it — it lets nobody list or read the directory, only pass
#    through to what is already world-readable beneath (the checkout and tree keep their modes).
step "worker-traversable path to the checkout and kernel tree"
kernel_src="${KDIVE_KERNEL_SRC:-${HOME}/src/linux}"
for target in "${repo_root}" "${kernel_src}"; do
  [[ -d "${target}" ]] || continue
  dir="${target}"
  while [[ "${dir}" != "/" ]]; do
    if [[ ! "$(stat -c '%A' "${dir}")" =~ x$ ]]; then
      chmod o+x "${dir}"
      echo "  ${dir}: o+x"
    fi
    dir="$(dirname -- "${dir}")"
  done
done

# 9. Venv wiring for build-fs and the kdump capture path: symlink the distro guestfs binding
#    into the venv (a uv venv has no system-site-packages). The binding is a C extension built
#    for the distro Python, so it only imports when the system and venv Python minor versions
#    match; otherwise leave a note. Ubuntu 26.04 and Fedora 44 both ship 3.14, the project
#    Python, so both link. Enterprise Linux does not — EL9 is 3.9 and EL10 is 3.12 — so an EL
#    host takes the note branch and loses only kdump capture. This is the venv remedy of
#    scripts/check-setup-deps.sh alone — `-y` there also installs the dev-tier tooling
#    (shellcheck, ...) a contributor wants and an operator host does not.
step "venv libguestfs binding"
venv_python="${repo_root}/.venv/bin/python"
sys_minor="$(/usr/bin/python3 -c 'import sys; print(sys.version_info[1])')"
venv_minor="$("${venv_python}" -c 'import sys; print(sys.version_info[1])')"
if [[ "${sys_minor}" != "${venv_minor}" ]]; then
  echo "system python3 is 3.${sys_minor} but the venv is 3.${venv_minor}; the distro guestfs binding"
  echo "cannot be shared. Kernel-under-test builds (build-fs) and kdump capture need it; see"
  echo "docs/operating/runbooks/four-method-live-run.md section 4b."
else
  guestfs_site="$(/usr/bin/python3 -c 'import os, guestfs; print(os.path.dirname(guestfs.__file__))')"
  venv_site="$("${venv_python}" -c 'import sysconfig; print(sysconfig.get_path("platlib"))')"
  ln -sfn "${guestfs_site}/guestfs.py" "${venv_site}/guestfs.py"
  ln -sfn "${guestfs_site}"/libguestfsmod*.so "${venv_site}/"
  "${venv_python}" -c 'import guestfs, drgn; print("guestfs + drgn importable from the venv")'
fi

cat <<EOF

host prepared for local-libvirt.

Next:
  1. Log out and back in (or 'exec su -l ${USER}') so the new groups apply
     (${host_groups[*]}, kdive-live-control, kdive-live-libvirt).
  2. Bring up:    ${example_dir}/up.sh
     (runs the preflight first; a remaining 'import guestfs, drgn' WARN affects only kdump capture)
  3. Guest image: ${example_dir}/build-image.sh fedora-kdive-ready-44
EOF
