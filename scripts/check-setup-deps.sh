#!/usr/bin/env bash
# Report host packages KDIVE needs, grouped by tier, with a single distro-specific install hint per
# tier. Reports by default; with `-y`/--yes (or an interactive [y/N] accept) it also remediates —
# installs missing distro packages (via sudo when non-root) and symlinks the libguestfs binding into
# the venv (ADR-0393). A non-TTY run without `-y` stays report-only. `-y` provisioning callers are
# expected to run as root or with passwordless sudo. Set KDIVE_OS_RELEASE to an alternate os-release
# file, KDIVE_KVM_NODE/KDIVE_PYTHON/KDIVE_GUESTFS_SYS_SITE/KDIVE_SYSTEM_PY_MINOR to override probes
# (used by the tests).
set -euo pipefail

# -y / --yes auto-accepts every fix offer (for `just setup` and provisioning scripts).
ASSUME_YES=0
while (($#)); do
  case "$1" in
  -y | --yes) ASSUME_YES=1 ;;
  -h | --help)
    printf "usage: check-setup-deps.sh [-y|--yes]\n"
    exit 0
    ;;
  *)
    printf "unknown argument: %s\n" "$1" >&2
    exit 2
    ;;
  esac
  shift
done
readonly ASSUME_YES

readonly OS_RELEASE_FILE="${KDIVE_OS_RELEASE:-/etc/os-release}"

# The worker imports the libguestfs binding from the project venv, not system python3, so the
# future-tier binding probe must ask the same interpreter the worker uses (else a binding present
# system-wide but absent from the venv reports a false green, #1328). Mirror check-local-libvirt.sh:
# prefer the .venv sibling of this script when present (in-repo dev loop), honor a KDIVE_PYTHON
# override (host-services deployment), and fall back to system python3 before the venv exists.
#
# Path derived via parameter expansion, not `dirname` — the script's own tests run it under a
# stubbed PATH with no coreutils. BASH_SOURCE[0] is often relative (`bash scripts/check-setup-deps.sh`
# from the repo root gives a single-slash path), so anchor it to $PWD first (a builtin, unlike
# dirname) to stay CWD-independent; without this the two strips below yield `scripts/.venv/...`,
# which misses the venv and silently falls back to system python3. `${var%/*}` strips one trailing
# component; two applications (script filename, then the scripts/ dir) give the repo root.
_repo_venv_py="${BASH_SOURCE[0]}"
[[ "${_repo_venv_py}" == /* ]] || _repo_venv_py="${PWD}/${_repo_venv_py}"
_repo_venv_py="${_repo_venv_py%/*}"
_repo_venv_py="${_repo_venv_py%/*}/.venv/bin/python"
if [[ -z "${KDIVE_PYTHON:-}" && -x "${_repo_venv_py}" ]]; then
  readonly PY="${_repo_venv_py}"
else
  readonly PY="${KDIVE_PYTHON:-python3}"
fi
unset _repo_venv_py

# Per-tier accumulators: *_commands feed the human-readable summary line,
# *_packages feed the distro install hint. manual_hints holds install commands
# for tooling that distros do not package (uv, prek, just).
# The *_packages arrays are written and read only through namerefs
# (note_package / report_tier), which shellcheck cannot follow — hence the
# SC2034 "unused" suppressions on those declarations.
required_commands=()
# shellcheck disable=SC2034
required_packages=()
recommended_commands=()
# shellcheck disable=SC2034
recommended_packages=()
future_commands=()
# shellcheck disable=SC2034
future_packages=()
manual_hints=()

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

load_distro_id() {
  local id="" id_like=""
  if [[ -r "${OS_RELEASE_FILE}" ]]; then
    # shellcheck disable=SC1090,SC1091
    source "${OS_RELEASE_FILE}"
    id="${ID:-}"
    id_like="${ID_LIKE:-}"
  fi
  case " ${id} ${id_like} " in
  *" fedora "* | *" rhel "* | *" centos "*) printf "fedora" ;;
  *" debian "* | *" ubuntu "*) printf "debian" ;;
  *" arch "*) printf "arch" ;;
  *" opensuse "* | *" suse "*) printf "opensuse" ;;
  *) printf "unknown" ;;
  esac
}

# Map a logical dependency name to its package name on the detected distro.
# Names that are identical everywhere fall through to the default branch.
package_for() {
  local name="$1" distro="$2"
  case "${name}:${distro}" in
  pkg-config:fedora) printf "pkgconf-pkg-config" ;;
  pkg-config:arch) printf "pkgconf" ;;
  libvirt-headers:fedora | libvirt-headers:opensuse) printf "libvirt-devel" ;;
  libvirt-headers:arch) printf "libvirt" ;;
  libvirt-headers:*) printf "libvirt-dev" ;;
  python-headers:fedora | python-headers:opensuse) printf "python3-devel" ;;
  python-headers:arch) printf "python" ;;
  python-headers:*) printf "python3-dev" ;;
  shellcheck:fedora) printf "ShellCheck" ;;
  libelf-headers:fedora) printf "elfutils-libelf-devel" ;;
  libelf-headers:opensuse) printf "libelf-devel" ;;
  libelf-headers:arch) printf "libelf" ;;
  libelf-headers:*) printf "libelf-dev" ;;
  # libdw is the DWARF debuginfo library from elfutils; drgn's vendored libdrgn links against
  # it when building from source (wheel-less arches). On Fedora it lives inside elfutils-devel;
  # Arch's libelf package already includes libdw.
  libdw-headers:fedora) printf "elfutils-devel" ;;
  libdw-headers:opensuse) printf "libdw-devel" ;;
  libdw-headers:arch) printf "libelf" ;;
  libdw-headers:*) printf "libdw-dev" ;;
  # libkdumpfile lets drgn open kdump-COMPRESSED vmcores; without it the local-libvirt kdump
  # capture path fails "drgn was built without libkdumpfile support" even though ELF cores read.
  libkdumpfile-headers:fedora | libkdumpfile-headers:opensuse) printf "libkdumpfile-devel" ;;
  libkdumpfile-headers:arch) printf "libkdumpfile" ;;
  libkdumpfile-headers:*) printf "libkdumpfile-dev" ;;
  # The libguestfs Python binding — required for the local-libvirt kdump capture path (ADR-0203).
  # Fedora/RHEL/openSUSE keep the historical `python3-libguestfs` name; Debian/Ubuntu renamed to
  # `python3-guestfs` (POWER host bring-up runbook, §1). Not pip-installable; system package only.
  python3-guestfs:fedora | python3-guestfs:opensuse) printf "python3-libguestfs" ;;
  python3-guestfs:arch) printf "libguestfs" ;;
  python3-guestfs:*) printf "python3-guestfs" ;;
  node:opensuse) printf "nodejs-default" ;;
  node:*) printf "nodejs" ;;
  npm:opensuse) printf "npm-default" ;;
  docker:debian) printf "docker.io" ;;
  docker:*) printf "docker" ;;
  qemu-system-x86_64:opensuse) printf "qemu-x86" ;;
  qemu-system-x86_64:*) printf "qemu-system-x86" ;;
  qemu-system-ppc64:opensuse) printf "qemu-ppc" ;;
  qemu-system-ppc64:*) printf "qemu-system-ppc" ;;
  qemu-img:debian) printf "qemu-utils" ;;
  qemu-img:opensuse) printf "qemu-tools" ;;
  qemu-img:*) printf "qemu-img" ;;
  virsh:debian) printf "libvirt-clients" ;;
  virsh:arch) printf "libvirt" ;;
  virsh:*) printf "libvirt-client" ;;
  virt-builder:debian | virt-tar-out:debian | virt-make-fs:debian | guestfish:debian) printf "libguestfs-tools" ;;
  virt-builder:arch | virt-tar-out:arch | virt-make-fs:arch | guestfish:arch) printf "libguestfs" ;;
  virt-builder:* | virt-tar-out:* | virt-make-fs:* | guestfish:*) printf "guestfs-tools" ;;
  gcc-or-clang:*) printf "gcc" ;;
  # The .deb / .rpm named `libtool` provides `libtoolize`; there is no standalone `libtoolize`
  # package to install. Rename here rather than probe `libtool` (which is a per-project script
  # generated at build time, not a distro binary).
  libtoolize:*) printf "libtool" ;;
  *) printf "%s" "${name}" ;;
  esac
}

# Record a missing distro-packaged dependency under the given tier, de-duplicating
# the package so the guestfish/virt-* family collapses to one install entry.
note_package() {
  local tier="$1" label="$2" package="$3"
  # shellcheck disable=SC2178  # namerefs to per-tier arrays, not string assignments
  local -n cmds="${tier}_commands" pkgs="${tier}_packages"
  cmds+=("${label}")
  local existing
  for existing in ${pkgs[@]+"${pkgs[@]}"}; do
    [[ "${existing}" == "${package}" ]] && return
  done
  pkgs+=("${package}")
}

# Record a missing tool that distros do not package, with its own install command.
note_manual() {
  local tier="$1" label="$2" instruction="$3"
  # shellcheck disable=SC2178  # nameref to a per-tier array, not a string assignment
  local -n cmds="${tier}_commands"
  cmds+=("${label}")
  manual_hints+=("${label}: ${instruction}")
}

require_command() {
  local tier="$1" name="$2" distro="$3"
  command_exists "${name}" || note_package "${tier}" "${name}" "$(package_for "${name}" "${distro}")"
}

require_tool() {
  local tier="$1" name="$2" instruction="$3"
  command_exists "${name}" || note_manual "${tier}" "${name}" "${instruction}"
}

# A header package exposes no binary, so probe pkg-config instead of the PATH.
require_header() {
  local tier="$1" label="$2" module="$3" distro="$4"
  command_exists pkg-config && pkg-config --exists "${module}" 2>/dev/null && return
  note_package "${tier}" "${label}" "$(package_for "${label}" "${distro}")"
}

join_by_comma() {
  local joined="" item
  for item in "$@"; do
    if [[ -z "${joined}" ]]; then
      joined="${item}"
    else
      joined="${joined}, ${item}"
    fi
  done
  printf "%s" "${joined}"
}

print_install_hint() {
  local distro="$1"
  shift
  case "${distro}" in
  fedora) printf "    dnf install %s\n" "$*" ;;
  debian) printf "    apt install %s\n" "$*" ;;
  arch) printf "    pacman -S %s\n" "$*" ;;
  opensuse) printf "    zypper install %s\n" "$*" ;;
  *) printf "    install with your distribution package manager: %s\n" "$*" ;;
  esac
}

report_tier() {
  local heading="$1" tier="$2" distro="$3"
  # shellcheck disable=SC2178  # namerefs to per-tier arrays, not string assignments
  local -n cmds="${tier}_commands" pkgs="${tier}_packages"
  ((${#cmds[@]} == 0)) && return
  printf "\n%s missing: %s\n" "${heading}" "$(join_by_comma "${cmds[@]}")" >&2
  if ((${#pkgs[@]} > 0)); then
    print_install_hint "${distro}" "${pkgs[@]}" >&2
  fi
}

# The arches KDIVE can provision. A host arch outside this set has no native qemu KDIVE knows how
# to name, so it is reported as unsupported rather than defaulted to x86.
readonly SUPPORTED_ARCHES=(ppc64le x86_64)

# The QEMU system-emulator binary is arch-named, and the name is not a plain `uname -m`:
# ppc64le maps to `qemu-system-ppc64` (not `-ppc64le`; POWER has no such binary). Only the
# provisionable arches are mapped; an unknown arch yields the empty string.
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

# Arches PyPI ships no prebuilt wheels for. Their Python C/Rust extension deps
# (pydantic-core) and the just/prek tools build from source, so a Rust toolchain is
# required. x86_64 gets wheels and needs no Rust; an empty/unknown host arch (e.g. a
# restricted-PATH environment without `uname`) is treated as wheel-ful, so no false
# Rust requirement is raised where the arch cannot be determined.
readonly WHEELLESS_ARCHES=(ppc64le)

arch_needs_rust() {
  local candidate
  for candidate in "${WHEELLESS_ARCHES[@]}"; do
    [[ "${candidate}" == "$1" ]] && return 0
  done
  return 1
}

# Report cross-arch guest availability (report-only): the native arch runs under KVM, every
# foreign arch under TCG. For each supported arch other than the host's, say whether its qemu
# emulator is present (TCG guests available here) or name the exact package that enables it. An
# unsupported host arch gets one explicit line instead of an x86 fallback. Prints to stdout — it
# is informational, not a missing-dependency report.
# The KVM device node backing native acceleration; override for tests (mirrors check-local-libvirt.sh).
readonly KVM_NODE="${KDIVE_KVM_NODE:-/dev/kvm}"

print_cross_arch_advisory() {
  local host="$1" distro="$2" arch binary pkg native
  if ! arch_is_supported "${host}"; then
    printf "\nhost arch %s is not a supported kdive provisioning arch (supported: %s)\n" \
      "${host}" "$(join_by_comma "${SUPPORTED_ARCHES[@]}")"
    return
  fi
  printf "\nHost architecture: %s (supported kdive provisioning arch)\n" "${host}"
  # Native (host) arch first: KVM-accelerated when the emulator + /dev/kvm are both present.
  # /dev/kvm accessibility is only what the probe proves, not that KVM will accelerate;
  # check-local-libvirt.sh is the authoritative gate.
  native="$(qemu_binary_for_arch "${host}")"
  if command_exists "${native}"; then
    if [[ -r "${KVM_NODE}" && -w "${KVM_NODE}" ]]; then
      printf "  guest arch %s: available natively via %s (/dev/kvm accessible — KVM)\n" "${host}" "${native}"
    else
      printf "  guest arch %s: native emulator present, /dev/kvm not accessible — runs under TCG until KVM is enabled\n" "${host}"
    fi
  else
    printf "  guest arch %s: not available; install %s for native guests\n" "${host}" "$(package_for "${native}" "${distro}")"
  fi
  # Foreign arches: TCG only.
  for arch in "${SUPPORTED_ARCHES[@]}"; do
    [[ "${arch}" == "${host}" ]] && continue
    binary="$(qemu_binary_for_arch "${arch}")"
    [[ -z "${binary}" ]] && continue # a supported arch with no binary mapping: skip, don't advise ""
    if command_exists "${binary}"; then
      printf "\nguest arch %s: available via TCG only (%s)\n" "${arch}" "${binary}"
    else
      pkg="$(package_for "${binary}" "${distro}")"
      printf "\nguest arch %s: not available; install %s for TCG guests\n" "${arch}" "${pkg}"
    fi
  done
}

distro="$(load_distro_id)"
# Guard the substitution so an absent `uname` (restricted-PATH tests) does not trip `set -e`.
host_arch="$(uname -m 2>/dev/null || true)"

# The system dir holding the libguestfs binding: Debian's version-agnostic dist-packages, falling
# back to the owning interpreter's purelib (Fedora) — the exact logic in runbook §4b. Overridable.
guestfs_sys_site() {
  local d="${KDIVE_GUESTFS_SYS_SITE:-/usr/lib/python3/dist-packages}"
  [[ -e "${d}/guestfs.py" ]] ||
    d="$(/usr/bin/python3 -c 'import sysconfig; print(sysconfig.get_path("purelib"))' 2>/dev/null || true)"
  printf "%s" "${d}"
}

# guestfs is the one future-tier binding whose remedy is more than an apt install: it is a
# distro-packaged binding (not pip-installable) the worker imports from the project venv, and a
# uv-created .venv has no system-site-packages, so even with python3-guestfs installed the venv
# cannot import it until guestfs.py + libguestfsmod*.so are symlinked in. So the entry is
# three-state, keyed on BOTH package presence and venv import (importability alone conflates them):
# absent -> install the package; unlinked (present but not importable) -> the symlink remedy, never
# an install hint for an already-installed package; ok -> nothing.

# Set GUESTFS_STATE only (no accumulator side effects) so it is safe to re-run for a state refresh.
detect_guestfs_state() {
  local site
  site="$(guestfs_sys_site)"
  if [[ ! -e "${site}/guestfs.py" ]]; then
    GUESTFS_STATE=absent
  elif command_exists "${PY}" && "${PY}" -c "import guestfs" 2>/dev/null; then
    GUESTFS_STATE=ok
  else
    GUESTFS_STATE=unlinked
  fi
}

# Detect + report; the reporting appends to accumulators, so this runs only inside probe_all.
probe_guestfs() {
  local distro="$1"
  detect_guestfs_state
  case "${GUESTFS_STATE}" in
  absent) note_package future python3-guestfs "$(package_for python3-guestfs "${distro}")" ;;
  unlinked) manual_hints+=("python3-guestfs: present system-wide but not importable in the venv — symlink guestfs.py + libguestfsmod*.so into the venv site-packages (a uv venv has no system-site-packages) — see docs/operating/runbooks/four-method-live-run.md section 4b") ;;
  esac
}

# Populate every tier accumulator from a fresh probe of the host. Called once at startup and again
# after fixes (re-verification), so it resets the arrays first (bash caches command lookups, so the
# caller runs `hash -r` before the second call). distro/host_arch are resolved once above (they do
# not change) and read here as globals.
probe_all() {
  # The *_packages arrays are written/read only through namerefs (note_package/report_tier),
  # which shellcheck cannot follow — hence the SC2034 suppressions (as on their declarations).
  required_commands=()
  # shellcheck disable=SC2034
  required_packages=()
  recommended_commands=()
  # shellcheck disable=SC2034
  recommended_packages=()
  future_commands=()
  # shellcheck disable=SC2034
  future_packages=()
  manual_hints=()

  # REQUIRED — `uv sync` and the core dev loop fail without these.
  require_tool required uv "curl -LsSf https://astral.sh/uv/install.sh | sh"
  require_command required pkg-config "${distro}"
  require_header required libvirt-headers libvirt "${distro}"
  # libvirt-python and any wheel-less C/Rust extension (e.g. pydantic-core, grpcio on
  # arches without prebuilt wheels) compile against the Python development headers.
  require_header required python-headers python3 "${distro}"
  # On wheel-less arches (ppc64le) pydantic-core and the just/prek tools build from source,
  # so a Rust toolchain is required. rustup provides both rustc and cargo, so a single hint
  # covers either being absent. x86_64 has prebuilt wheels and needs no Rust toolchain.
  if arch_needs_rust "${host_arch}" && { ! command_exists rustc || ! command_exists cargo; }; then
    note_manual required "rustc/cargo" "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
  fi

  # RECOMMENDED — needed to reproduce the full local CI gate.
  require_command recommended git "${distro}"
  require_command recommended make "${distro}"
  require_command recommended shellcheck "${distro}"
  require_command recommended shfmt "${distro}"
  require_tool recommended just "uv tool install rust-just"
  require_tool recommended prek "uv tool install prek"
  require_command recommended docker "${distro}"
  command_exists node || command_exists nodejs ||
    note_package recommended node "$(package_for node "${distro}")"
  require_command recommended npm "${distro}"

  # FUTURE — live_vm and kernel-build milestones; warn only, never block setup.
  future_cmds=(virsh gdb crash virt-builder virt-tar-out virt-make-fs guestfish qemu-img bc flex bison)
  # Require the host's native qemu emulator only on a supported host arch (an unsupported arch has
  # no native qemu KDIVE can name; the cross-arch advisory below reports that instead).
  native_qemu="$(qemu_binary_for_arch "${host_arch}")"
  if arch_is_supported "${host_arch}" && [[ -n "${native_qemu}" ]]; then
    future_cmds+=("${native_qemu}")
  fi
  for cmd in "${future_cmds[@]}"; do
    require_command future "${cmd}" "${distro}"
  done
  command_exists gcc || command_exists clang ||
    note_package future "gcc or clang" "$(package_for gcc-or-clang "${distro}")"
  require_header future libelf-headers libelf "${distro}"

  # The four-method live run needs the libguestfs Python binding on the WORKER host (kdump
  # capture; ADR-0203) plus libdw + libkdumpfile so drgn can build with debuginfo support and
  # open kdump-compressed vmcores (see four-method-live-run.md §4b and the POWER runbook §1).
  require_header future libdw-headers libdw "${distro}"
  require_header future libkdumpfile-headers libkdumpfile "${distro}"
  probe_guestfs "${distro}"

  # Wheel-less arches (ppc64le) build drgn from source — its vendored libdrgn uses autotools, so
  # `uv sync --group live` fails at `autoreconf` without autoconf/automake/libtool. libtool the
  # .deb ships `libtoolize` (no `libtool` binary); the actual libtool script is generated per
  # project, so probe `libtoolize` — its presence is what drgn's build actually needs.
  if arch_needs_rust "${host_arch}"; then
    for cmd in autoconf automake libtoolize; do
      require_command future "${cmd}" "${distro}"
    done
  fi
}

# A fix runs only on an explicit opt-in: `-y` auto-accepts; an interactive TTY prompts (default No);
# a non-TTY without `-y` never fixes (report-only — the invariant CI and the tests rely on).
offer_accepted() {
  ((ASSUME_YES)) && return 0
  [[ -t 0 ]] || return 1
  local ans
  printf "%s [y/N] " "$1" >&2
  read -r ans
  [[ "${ans}" == [yY]* ]]
}

# Run a command with the right sudo flavor for the mode. Interactive => plain `sudo` (a password
# prompt is desired when a human just consented); `-y`/non-TTY => `sudo -n` (never block). A
# credential pre-flight separates an escalation failure from a package failure. Returns 77 when
# escalation is unavailable so the caller can skip with a message instead of hanging.
run_privileged() {
  ((EUID == 0)) && {
    "$@"
    return
  }
  command_exists sudo || {
    printf "  need root: sudo not found — run as root to: %s\n" "$*" >&2
    return 77
  }
  if ((ASSUME_YES)); then
    sudo -n true 2>/dev/null || {
      printf "  re-run as root or with passwordless sudo to install: %s\n" "$*" >&2
      return 77
    }
    sudo -n "$@"
  else
    sudo -v || {
      printf "  sudo authentication failed; re-run as root to install: %s\n" "$*" >&2
      return 77
    }
    sudo "$@"
  fi
}

# Offer to install a tier's missing distro packages. Sets FIX_ATTEMPTED when it runs anything.
# Commands are built as argv arrays and passed straight to run_privileged (no `bash -c`), so the
# non-interactive install flag is explicit and nothing depends on a shell being on PATH.
maybe_install_tier() {
  local tier="$1" distro="$2"
  # shellcheck disable=SC2178  # nameref to a per-tier array, not a string assignment
  local -n pkgs="${tier}_packages"
  ((${#pkgs[@]})) || return 0
  offer_accepted "Install ${tier} packages (${pkgs[*]})?" || return 0

  local -a refresh_cmd=() install_cmd=()
  case "${distro}" in
  debian) refresh_cmd=(apt-get update) && install_cmd=(apt-get install -y "${pkgs[@]}") ;;
  fedora) install_cmd=(dnf install -y "${pkgs[@]}") ;;
  # Arch uses plain `pacman -S` (no `-Sy`): a bare `-Sy <pkg>` leaves the unsupported partial-upgrade
  # state on a non-fresh host, and `-Syu` would surprise-upgrade the whole system (ADR-0393).
  arch) install_cmd=(pacman -S --noconfirm "${pkgs[@]}") ;;
  opensuse) install_cmd=(zypper --non-interactive install "${pkgs[@]}") ;;
  *)
    printf "  no auto-install for this distro; install manually: %s\n" "${pkgs[*]}" >&2
    return 0
    ;;
  esac

  FIX_ATTEMPTED=1
  # A refresh failure is reported and non-fatal — do not short-circuit (that would swallow the more
  # informative install failure). Only Debian needs an index refresh on a fresh host.
  if ((${#refresh_cmd[@]})); then
    run_privileged "${refresh_cmd[@]}" ||
      printf "  package index refresh failed; attempting install anyway\n" >&2
  fi
  run_privileged "${install_cmd[@]}" ||
    printf "  package set failed to install: %s\n" "${pkgs[*]}" >&2
}

# Render a tier report then offer its fix. Used once per phase in the main flow.
report_and_fix_tier() {
  local heading="$1" tier="$2" distro="$3"
  report_tier "${heading}" "${tier}" "${distro}"
  maybe_install_tier "${tier}" "${distro}"
}

# Offer the guestfs venv symlink (the `unlinked` state only). Keeps the venv isolated (symlink, not
# --system-site-packages). No sudo. Sets FIX_ATTEMPTED on a successful link so the re-check clears
# the hint. Skips when ${PY} is not a real venv (never symlink into the system interpreter) or the
# system/venv Python minor versions differ (fail loud, never a broken link).
maybe_link_guestfs() {
  [[ "${GUESTFS_STATE}" == unlinked ]] || return 0
  "${PY}" -c 'import sys; raise SystemExit(0 if sys.prefix != sys.base_prefix else 1)' 2>/dev/null || {
    printf "  guestfs: %s is not a venv — skip (symlink only into an isolated venv)\n" "${PY}" >&2
    return 0
  }
  offer_accepted "Symlink the libguestfs binding into the venv?" || return 0
  local site sys_site vmin smin so
  site="$("${PY}" -c 'import sysconfig; print(sysconfig.get_path("purelib"))')"
  sys_site="$(guestfs_sys_site)"
  vmin="$("${PY}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  # The binding is built for the system interpreter (the Ansible role / runbook pin /usr/bin/python3);
  # its minor must match the venv's or the .so is ABI-incompatible. An empty smin means we cannot
  # determine the system interpreter — report that distinctly rather than as a fake "mismatch".
  smin="${KDIVE_SYSTEM_PY_MINOR:-$(/usr/bin/python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)}"
  if [[ -z "${smin}" ]]; then
    printf "  guestfs: cannot determine the system Python (for the ABI check) — skipping the symlink\n" >&2
    return 0
  fi
  if [[ "${vmin}" != "${smin}" ]]; then
    printf "  guestfs ABI mismatch: system python %s vs venv %s — not linking\n" "${smin}" "${vmin}" >&2
    return 0
  fi
  # `ln -sf` is idempotent (a re-run does not abort on "File exists"). Surface a link failure
  # (read-only venv, ENOSPC, permission) instead of swallowing it, so a consented fix is observable.
  local linked=1
  ln -sf "${sys_site}/guestfs.py" "${site}/" || linked=0
  for so in "${sys_site}"/libguestfsmod*.so; do
    [[ -e "${so}" ]] && { ln -sf "${so}" "${site}/" || linked=0; }
  done
  ((linked)) || printf "  guestfs: could not symlink the binding into %s (check permissions/space)\n" "${site}" >&2
  FIX_ATTEMPTED=1
}

# The three tiers, in report order, as `heading:tier` pairs (headings carry no colon). Single source
# of truth for both the initial report+fix pass and the post-fix re-check.
readonly TIER_SPECS=(
  "Required dependencies:required"
  "Recommended dependencies (full local CI):recommended"
  "Future dependencies (live_vm / kernel build):future"
)

FIX_ATTEMPTED=0
probe_all
for spec in "${TIER_SPECS[@]}"; do
  report_and_fix_tier "${spec%:*}" "${spec##*:}" "${distro}"
done
detect_guestfs_state # refresh state so a just-installed python3-guestfs flips absent -> unlinked
maybe_link_guestfs   # separate prompt; sets FIX_ATTEMPTED on a successful link

if ((FIX_ATTEMPTED)); then
  hash -r   # drop bash's cached command lookups so just-installed binaries are found
  probe_all # rebuild the accumulators from post-fix state
  printf "\n=== re-checking after fixes ===\n" >&2
  for spec in "${TIER_SPECS[@]}"; do
    report_tier "${spec%:*}" "${spec##*:}" "${distro}"
  done
fi

print_cross_arch_advisory "${host_arch}" "${distro}"

if ((${#manual_hints[@]} > 0)); then
  printf "\nTooling not provided by your distribution:\n" >&2
  printf "    %s\n" "${manual_hints[@]}" >&2
fi

if ((${#required_commands[@]} > 0)); then
  printf "\nInstall the required dependencies from a privileged shell, then rerun: just setup\n" >&2
  exit 1
fi

if ((${#recommended_commands[@]} + ${#future_commands[@]} > 0)); then
  printf "\nRequired dependencies are present; optional items above are not yet needed.\n"
else
  printf "Setup dependencies are present.\n"
fi
