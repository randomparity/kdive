#!/usr/bin/env bash
# Report whether this host can run the local-libvirt provider. Report-only: never
# installs, never escalates. Each runtime probe is a small function so tests can drive
# pass/fail via PATH stubs (virsh, id) and the KDIVE_KVM_NODE override. Exit 1 if any
# required check fails. Run before deploying; the service `doctor` covers post-deploy.
set -euo pipefail

readonly KVM_NODE="${KDIVE_KVM_NODE:-/dev/kvm}"
# The worker imports drgn + the libguestfs binding from the project venv, not system
# python3. Probe the same interpreter the worker uses. Prefer the .venv sibling of this
# script when present (in-repo dev loop) so `just check-local-libvirt` needs no env var;
# fall back to system python3, which a host-services deployment overrides via
# KDIVE_PYTHON=/opt/kdive/.venv/bin/python (or similar).
#
# Path derived via parameter expansion, not `dirname` — the script's own tests run it under a
# stubbed PATH containing only the test stubs (no coreutils), so an external `dirname` call
# fails. `${var%/*}` strips the trailing path component; two applications on an absolute
# BASH_SOURCE[0] give the repo root, then append `.venv/bin/python`.
_repo_venv_py="${BASH_SOURCE[0]%/*}"
_repo_venv_py="${_repo_venv_py%/*}/.venv/bin/python"
if [[ -z "${KDIVE_PYTHON:-}" && -x "${_repo_venv_py}" ]]; then
  readonly PY="${_repo_venv_py}"
else
  readonly PY="${KDIVE_PYTHON:-python3}"
fi
unset _repo_venv_py
# runs.install stages the kernel/initrd here before booting the System; must be writable
# by the worker user and live under a path the qemu user can traverse (see the boot check).
readonly INSTALL_STAGING="${KDIVE_INSTALL_STAGING:-/var/lib/kdive/install}"
# libguestfs builds its supermin appliance from a host kernel under this dir; Debian/Ubuntu ship
# /boot/vmlinuz-* root:0600, unreadable by a non-root worker, so build-fs fails (ADR-0222, #694).
# ppc64le names the kernel /boot/vmlinux-* (ELF, no 'z') instead — probe both patterns so a POWER
# host is not passed vacuously (#1156). Probe ALL present kernels — supermin selects by
# version-sort, not the running one. Override for tests.
readonly BOOT_DIR="${KDIVE_BOOT_DIR:-/boot}"
# Worker connection URI + effective uid drive the non-root-readability advisory (ADR-0223, #699):
# under qemu:///system, virtlogd/QEMU write root-owned files a non-root worker cannot read back.
# KDIVE_EFFECTIVE_UID overrides $EUID for tests, mirroring the KDIVE_KVM_NODE override.
readonly LIBVIRT_URI="${KDIVE_LIBVIRT_URI:-qemu:///system}"
readonly EFFECTIVE_UID="${KDIVE_EFFECTIVE_UID:-$EUID}"
fail=0

note_fail() {
  printf "\nFAIL  %s\n" "$1" >&2
  printf "  fix: %s\n\n" "$2" >&2
  fail=1
}

# Advisory: report-only guidance that does NOT fail the preflight (the named combination still
# works for the build and kdump-capture planes, so it must not reject an otherwise-ready host).
note_warn() {
  printf "\nWARN  %s\n" "$1" >&2
  printf "  fix: %s\n\n" "$2" >&2
}

note_ok() {
  printf " OK   %s\n" "$1" >&2
}

note_info() {
  printf "INFO  %s\n" "$1" >&2
}

# The arches KDIVE can provision. The qemu system-emulator binary is arch-named and NOT a plain
# `uname -m`: ppc64le maps to `qemu-system-ppc64` (POWER has no `-ppc64le` binary). A host arch
# outside this set has no native qemu KDIVE can name and is reported unsupported, not defaulted to
# x86 (the prior hardcode). Kept in sync with scripts/check-setup-deps.sh and arch_traits.py.
readonly SUPPORTED_ARCHES=(ppc64le x86_64)

qemu_binary_for_arch() {
  case "$1" in
  ppc64le) printf "qemu-system-ppc64" ;;
  x86_64) printf "qemu-system-x86_64" ;;
  *) printf "" ;;
  esac
}

arch_is_supported() {
  local candidate
  for candidate in "${SUPPORTED_ARCHES[@]}"; do
    [[ "${candidate}" == "$1" ]] && return 0
  done
  return 1
}

supported_arches_csv() {
  local out="" arch
  for arch in "${SUPPORTED_ARCHES[@]}"; do
    out="${out:+${out}, }${arch}"
  done
  printf "%s" "${out}"
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
  # vmlinuz-* on x86_64, vmlinux-* on ppc64le (#1156) — probe both so neither arch is missed.
  for k in "${BOOT_DIR}"/vmlinuz-* "${BOOT_DIR}"/vmlinux-*; do
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

printf "=== local-libvirt preflight ===\n\n" >&2

# ── Host / KVM ──────────────────────────────────────────────────────────────
printf "%s\n" "-- KVM & tools" >&2
if _has_kvm; then
  note_ok "${KVM_NODE} readable/writable"
else
  note_fail "${KVM_NODE} not readable/writable (KVM unavailable)" \
    "enable virtualization in BIOS and load kvm modules; ensure your user can access ${KVM_NODE}"
fi
for c in virsh qemu-img; do
  if _cmd "$c"; then
    note_ok "${c} found on PATH"
  else
    note_fail "${c} not found on PATH" \
      "install it via your distribution (see scripts/check-setup-deps.sh hints)"
  fi
done

# ── QEMU emulators ──────────────────────────────────────────────────────────
printf "\n%s\n" "-- QEMU emulators" >&2

# Require the host's native qemu emulator (arch-derived, not the old x86 hardcode) only on a
# supported host arch. An unsupported host arch cannot run native guests, so it is reported once
# rather than failed for a missing x86 emulator. Each supported foreign arch whose emulator is
# present is advertised as TCG-only (informational; the native arch runs under KVM).
host_arch="$(uname -m 2>/dev/null || true)"
native_qemu="$(qemu_binary_for_arch "${host_arch}")"
if arch_is_supported "${host_arch}" && [[ -n "${native_qemu}" ]]; then
  if _cmd "${native_qemu}"; then
    note_ok "${native_qemu} present (native KVM-HV for ${host_arch})"
  else
    note_fail "${native_qemu} not found on PATH" \
      "install it via your distribution (see scripts/check-setup-deps.sh hints)"
  fi
  for guest_arch in "${SUPPORTED_ARCHES[@]}"; do
    [[ "${guest_arch}" == "${host_arch}" ]] && continue
    foreign_qemu="$(qemu_binary_for_arch "${guest_arch}")"
    [[ -z "${foreign_qemu}" ]] && continue
    if _cmd "${foreign_qemu}"; then
      note_info \
        "guest arch ${guest_arch} available via TCG only (foreign emulator ${foreign_qemu} present; scaled by KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER)"
    fi
  done
else
  note_info "host arch ${host_arch} is not a supported kdive provisioning arch (supported: $(supported_arches_csv))"
fi

# Advisory (ADR-0349): fadump on POWER pseries needs QEMU >= 10.2 (the ibm,configure-kernel-dump
# RTAS). Report-only — an absent/old qemu-system-ppc64 does not fail this host (kdump is the spine
# and x86 is unaffected); it only tells an operator whether fadump systems can be provisioned here.
if _cmd qemu-system-ppc64; then
  _ppc_ver="$(qemu-system-ppc64 --version 2>/dev/null |
    sed -n 's/^QEMU emulator version \([0-9]*\)\.\([0-9]*\).*/\1 \2/p')"
  read -r _ppc_maj _ppc_min <<<"${_ppc_ver:-0 0}"
  if ((_ppc_maj > 10 || (_ppc_maj == 10 && _ppc_min >= 2))); then
    note_ok "qemu-system-ppc64 ${_ppc_maj}.${_ppc_min} implements pseries fadump (>= 10.2)"
  else
    note_warn \
      "qemu-system-ppc64 ${_ppc_maj}.${_ppc_min} predates QEMU 10.2, so pseries fadump is unavailable" \
      "upgrade QEMU to >= 10.2 to provision fadump systems here, or validate fadump on native POWER; kdump is unaffected"
  fi
fi

# ── libvirt connectivity ─────────────────────────────────────────────────────
printf "\n%s\n" "-- libvirt" >&2
if _in_libvirt_group; then
  note_ok "invoking user is in the 'libvirt' group"
else
  note_fail "invoking user is not in the 'libvirt' group" \
    "sudo usermod -aG libvirt \"\$USER\" and re-login"
fi
if _cmd virsh; then
  if _virsh_connects; then
    note_ok "virsh connects to qemu:///system"
  else
    note_fail "cannot connect to qemu:///system" \
      "start the libvirt daemon: systemctl enable --now virtqemud.socket (or libvirtd)"
  fi
  if _default_net_active; then
    note_ok "libvirt 'default' network is active"
  else
    note_fail "libvirt 'default' network is not active" \
      "virsh -c qemu:///system net-start default && virsh -c qemu:///system net-autostart default"
  fi
fi

# ── Worker venv & host paths ─────────────────────────────────────────────────
printf "\n%s\n" "-- Worker venv & host paths" >&2
if _venv_imports_kdump_deps; then
  note_ok "worker venv (${PY}) imports guestfs and drgn"
else
  note_fail \
    "worker venv (${PY}) cannot 'import guestfs, drgn' (local-libvirt kdump capture, ADR-0203)" \
    "uv sync --group live (drgn); install python3-libguestfs, then symlink its guestfs.py + libguestfsmod*.so into the venv site-packages (python versions must match) — see docs/operating/runbooks/four-method-live-run.md section 4b"
fi

if _host_kernels_readable; then
  note_ok "host kernels under ${BOOT_DIR} are readable"
else
  note_fail \
    "a host kernel under ${BOOT_DIR} (vmlinuz-* on x86_64, vmlinux-* on ppc64le) is not readable by this user (libguestfs build-fs appliance, ADR-0222)" \
    "run this preflight as the worker user; if Debian/Ubuntu (root:0600 kernels): sudo chmod 0644 ${BOOT_DIR}/vmlinu?-* (matches both arches; re-apply after kernel upgrades, or use dpkg-statoverride)"
fi

if _dir_writable "${INSTALL_STAGING}"; then
  note_ok "install staging ${INSTALL_STAGING} is writable"
else
  note_fail \
    "install staging ${INSTALL_STAGING} is not a directory writable by the worker user (KDIVE_INSTALL_STAGING; runs.install stages the kernel/initrd here)" \
    "create it writable under a world-traversable path (NOT \$HOME, which a 0700 mode hides from the qemu user that boots the VM): sudo install -d -o \"\$USER\" ${INSTALL_STAGING} — see docs/operating/runbooks/four-method-live-run.md section 4b"
fi

if [[ "${LIBVIRT_URI}" == "qemu:///system" && "${EFFECTIVE_UID}" -ne 0 ]]; then
  note_warn \
    "non-root worker under qemu:///system: boot-confirmation and host_dump capture cannot read the root-owned console log / core that virtlogd/QEMU write (ADR-0223, #699)" \
    "run the worker as root, set KDIVE_LIBVIRT_URI=qemu:///session (worker-owned QEMU), or grant the worker group read access to the libvirt/virtlogd output; build and kdump capture still work as-is"
fi

# ── Summary ──────────────────────────────────────────────────────────────────
if ((fail)); then
  printf "\n=== local-libvirt host is NOT ready (see FAIL entries above) ===\n" >&2
  exit 1
fi
printf "\n=== local-libvirt host is ready ===\n" >&2
