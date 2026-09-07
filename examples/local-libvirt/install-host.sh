#!/usr/bin/env bash
# Prepare a fresh Debian/Ubuntu host for the local-libvirt developer setup. Idempotent; safe to
# re-run. Installs the host packages, adds the invoking user to the libvirt/kvm/docker groups,
# makes the host kernels readable for the libguestfs appliance, installs uv, builds the project
# venv, creates the /var/lib/kdive directories, and wires the kdump libguestfs binding.
#
#   examples/local-libvirt/install-host.sh
#
# Then log out and back in (the new group memberships take effect on a new login shell) and
# continue with examples/local-libvirt/up.sh. The Fedora path is the manual Step 1 of the
# local-libvirt walkthrough (docs/operating/providers/local-libvirt-walkthrough.md); this script
# covers only apt-based hosts.
set -euo pipefail

example_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${example_dir}/../.." && pwd)"

step() { printf '\n=== %s ===\n' "$1"; }

# 1. Distro and arch gates. Package names below are the Debian/Ubuntu set; the qemu emulator
#    package is arch-named (scripts/check-setup-deps.sh keeps the same mapping).
os_release="${KDIVE_OS_RELEASE:-/etc/os-release}"
distro_id="" distro_like=""
if [[ -r "${os_release}" ]]; then
  # shellcheck disable=SC1090
  distro_id="$(. "${os_release}" && printf '%s' "${ID:-}")"
  # shellcheck disable=SC1090
  distro_like="$(. "${os_release}" && printf '%s' "${ID_LIKE:-}")"
fi
case " ${distro_id} ${distro_like} " in
*" debian "* | *" ubuntu "*) ;;
*)
  echo "install-host.sh covers Debian/Ubuntu (apt) hosts; this host reports ID=${distro_id:-?}." >&2
  echo "Follow Step 1 of docs/operating/providers/local-libvirt-walkthrough.md instead, then" >&2
  echo "run ./scripts/check-setup-deps.sh -y for the venv wiring." >&2
  exit 2
  ;;
esac

host_arch="$(uname -m)"
case "${host_arch}" in
x86_64) qemu_package="qemu-system-x86" ;;
ppc64le) qemu_package="qemu-system-ppc" ;;
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

step "sudo (package install, group membership, /var/lib/kdive)"
# Prompt only when a password is needed: `sudo -v` insists on a terminal even where a NOPASSWD
# rule applies, which breaks a non-interactive run (ssh without a tty, nohup).
sudo -n true 2>/dev/null || sudo -v

# 2. Host packages: the operator set from the walkthrough (Step 1). The build toolchain is here
#    because the kernel under test is built on this host and uploaded on the build lane; the
#    worker itself never compiles kernel source (ADR-0316). `qemu-kvm` is deliberately absent —
#    the arch emulator package provides KVM and the transitional name no longer exists on
#    Ubuntu 26.04. On ppc64le, pydantic-core and the just/prek CLIs build from source, so a Rust
#    toolchain is needed as well (rustup; see the cross-platform guide).
packages=(
  build-essential pkg-config libvirt-dev python3-dev
  libvirt-daemon-system libvirt-clients qemu-utils "${qemu_package}"
  libguestfs-tools python3-guestfs passt e2fsprogs
  gcc make flex bison bc libssl-dev libelf-dev rsync xz-utils git curl ca-certificates
  docker.io docker-compose-v2 gdb
)
step "apt packages (${#packages[@]})"
# apt-install.sh refreshes the index itself, escalates through sudo, and bounds each call with
# a timeout + retry (ADR-0566). The default per-call budget is sized for CI's small header set;
# this set pulls the libguestfs appliance and Docker, so give it more room.
KDIVE_APT_TIMEOUT_S="${KDIVE_APT_TIMEOUT_S:-600}" \
  "${repo_root}/scripts/apt-install.sh" "${packages[@]}"

# 3. Group membership. Takes effect on the next login shell, which is why the script ends with
#    a reminder instead of running the preflight (it would report the libvirt group as missing).
step "groups libvirt,kvm,docker for ${USER}"
sudo usermod -aG libvirt,kvm,docker "${USER}"

# 4. Host kernels. Debian/Ubuntu ship /boot/vmlinuz-* root:0600; libguestfs builds its
#    supermin appliance from one, so build-fs and the kdump harvest fail for a non-root user
#    until they are readable (ADR-0222, #694). 0640 root:kvm keeps the read scoped to the KVM
#    group, the same posture deploy/ansible/roles/live_vm_host applies. A kernel upgrade
#    installs a new 0600 file: re-run this script (or just this step) afterwards.
step "host kernels readable by the kvm group"
for kernel in /boot/vmlinuz-* /boot/vmlinux-*; do
  [[ -e "${kernel}" ]] || continue
  sudo chgrp kvm "${kernel}"
  sudo chmod 0640 "${kernel}"
  echo "  ${kernel}: root:kvm 0640"
done

# 5. uv, then the project venv. `--group live` adds drgn, which the kdump capture path imports
#    from the worker venv alongside the libguestfs binding wired in step 7.
step "uv"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
fi
uv --version

step "uv sync --group live (${repo_root})"
(cd "${repo_root}" && uv sync --group live)
"${repo_root}/.venv/bin/python" -m kdive --help >/dev/null

# 6. Worker host directories: world-traversable (not under $HOME, whose 0700 hides the staged
#    kernel from the qemu user), owned by the operating user.
step "/var/lib/kdive directories"
sudo install -d -o "${USER}" -m 0755 \
  /var/lib/kdive/install /var/lib/kdive/console /var/lib/kdive/rootfs/local /var/lib/kdive/build

# 7. Venv wiring for the kdump capture path. check-setup-deps.sh -y symlinks the distro
#    python3-guestfs binding into the venv (only when the system and venv Python minor versions
#    match — Ubuntu 26.04 ships 3.14, the project Python) and installs any missing dev-tier
#    packages. Its exit status is the report, not a gate: the kdump binding is optional for the
#    core lifecycle and the preflight below says which.
step "check-setup-deps.sh -y (venv libguestfs binding)"
(cd "${repo_root}" && ./scripts/check-setup-deps.sh -y) || true

cat <<EOF

host prepared for local-libvirt.

Next:
  1. Log out and back in (or 'exec su -l ${USER}') so the libvirt/kvm/docker groups apply.
  2. Preflight:  cd ${repo_root} && ./scripts/operations/check-local-libvirt.sh
     (a remaining 'import guestfs, drgn' FAIL affects only kdump capture; up.sh tolerates it)
  3. Bring up:   ${example_dir}/up.sh
  4. Guest image: ${example_dir}/build-image.sh fedora-kdive-ready-44
EOF
