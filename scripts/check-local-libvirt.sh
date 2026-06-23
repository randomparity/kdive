#!/usr/bin/env bash
# Report whether this host can run the local-libvirt provider. Report-only: never
# installs, never escalates. Each runtime probe is a small function so tests can drive
# pass/fail via PATH stubs (virsh, id) and the KDIVE_KVM_NODE override. Exit 1 if any
# required check fails. Run before deploying; the service `doctor` covers post-deploy.
set -euo pipefail

readonly KVM_NODE="${KDIVE_KVM_NODE:-/dev/kvm}"
# The worker imports drgn + the libguestfs binding from the project venv, not system
# python3. Probe the same interpreter the worker uses; override on a host-services
# deployment, e.g. KDIVE_PYTHON=/opt/kdive/.venv/bin/python.
readonly PY="${KDIVE_PYTHON:-python3}"
# runs.install stages the kernel/initrd here before booting the System; must be writable
# by the worker user and live under a path the qemu user can traverse (see the boot check).
readonly INSTALL_STAGING="${KDIVE_INSTALL_STAGING:-/var/lib/kdive/install}"
# libguestfs builds its supermin appliance from a host kernel under this dir; Debian/Ubuntu ship
# /boot/vmlinuz-* root:0600, unreadable by a non-root worker, so build-fs fails (ADR-0222, #694).
# Probe ALL present kernels — supermin selects by version-sort, not the running one. Override for
# tests.
readonly BOOT_DIR="${KDIVE_BOOT_DIR:-/boot}"
fail=0

note_fail() {
  printf "FAIL: %s\n" "$1" >&2
  printf "  fix: %s\n" "$2" >&2
  fail=1
}

_has_kvm() { [[ -r "${KVM_NODE}" && -w "${KVM_NODE}" ]]; }
_cmd() { command -v "$1" >/dev/null 2>&1; }
_in_libvirt_group() { [[ " $(id -nG 2>/dev/null) " == *" libvirt "* ]]; }
_virsh_connects() { virsh -c qemu:///system list >/dev/null 2>&1; }
_default_net_active() {
  local out
  out="$(virsh -c qemu:///system net-info default 2>/dev/null || true)"
  [[ "$out" == *"Active:"*[Yy]es* ]]
}
_venv_imports_kdump_deps() { "${PY}" -c "import guestfs, drgn" >/dev/null 2>&1; }
_host_kernels_readable() {
  local k found=0
  for k in "${BOOT_DIR}"/vmlinuz-*; do
    [[ -e "$k" ]] || continue # no-match glob stays literal under no-nullglob; skip it
    found=1
    [[ -r "$k" ]] || return 1
  done
  ((found)) || return 0 # no kernels present: unusual layout, do not false-fail
  return 0
}
_dir_writable() {
  local dir="$1" probe
  [[ -d "$dir" && -w "$dir" ]] || return 1
  probe="${dir}/.kdive-write-probe.$$"
  if : >"${probe}" 2>/dev/null; then
    rm -f "${probe}"
    return 0
  fi
  return 1
}

_has_kvm || note_fail "${KVM_NODE} not readable/writable (KVM unavailable)" \
  "enable virtualization in BIOS and load kvm modules; ensure your user can access ${KVM_NODE}"
for c in virsh qemu-system-x86_64 qemu-img; do
  _cmd "$c" || note_fail "$c not found on PATH" "install it via your distribution (see scripts/check-setup-deps.sh hints)"
done
_in_libvirt_group || note_fail "invoking user is not in the 'libvirt' group" \
  "sudo usermod -aG libvirt \"\$USER\" and re-login"
if _cmd virsh; then
  _virsh_connects || note_fail "cannot connect to qemu:///system" \
    "start the libvirt daemon: systemctl enable --now virtqemud.socket (or libvirtd)"
  _default_net_active || note_fail "libvirt 'default' network is not active" \
    "virsh -c qemu:///system net-start default && virsh -c qemu:///system net-autostart default"
fi

_venv_imports_kdump_deps || note_fail \
  "worker venv (${PY}) cannot 'import guestfs, drgn' (local-libvirt kdump capture, ADR-0203)" \
  "uv sync --group live (drgn); install python3-libguestfs, then symlink its guestfs.py + libguestfsmod*.so into the venv site-packages (python versions must match) — see docs/operating/runbooks/four-method-live-run.md section 4b"

_host_kernels_readable || note_fail \
  "a host kernel under ${BOOT_DIR} (vmlinuz-*) is not readable by this user (libguestfs build-fs appliance, ADR-0222)" \
  "run this preflight as the worker user; if Debian/Ubuntu (root:0600 kernels): sudo chmod 0644 /boot/vmlinuz-* (re-apply after kernel upgrades, or use dpkg-statoverride)"

_dir_writable "${INSTALL_STAGING}" || note_fail \
  "install staging ${INSTALL_STAGING} is not a directory writable by the worker user (KDIVE_INSTALL_STAGING; runs.install stages the kernel/initrd here)" \
  "create it writable under a world-traversable path (NOT \$HOME, which a 0700 mode hides from the qemu user that boots the VM): sudo install -d -o \"\$USER\" ${INSTALL_STAGING} — see docs/operating/runbooks/four-method-live-run.md section 4b"

if ((fail)); then
  printf "\nlocal-libvirt host is NOT ready (see failures above)\n" >&2
  exit 1
fi
printf "local-libvirt host is ready\n"
