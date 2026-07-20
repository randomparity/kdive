#!/usr/bin/env bash
# Report host packages KDIVE needs, grouped by tier, with a single distro-specific
# install hint per tier. Reports only — never installs and never escalates. Set
# KDIVE_OS_RELEASE to point at an alternate os-release file (used by the tests).
set -euo pipefail

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

# A distro-packaged Python binding is not pip-installable, so verify presence by asking the
# worker's venv interpreter (${PY}) to import it. Falls back to a note_package hint under the
# given tier if the import fails (module missing OR interpreter absent).
require_pyimport() {
  local tier="$1" label="$2" module="$3" distro="$4"
  command_exists "${PY}" && "${PY}" -c "import ${module}" 2>/dev/null && return
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
print_cross_arch_advisory() {
  local host="$1" distro="$2" arch binary pkg
  if ! arch_is_supported "${host}"; then
    printf "\nhost arch %s is not a supported kdive provisioning arch (supported: %s)\n" \
      "${host}" "$(join_by_comma "${SUPPORTED_ARCHES[@]}")"
    return
  fi
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
require_pyimport future python3-guestfs guestfs "${distro}"

# Wheel-less arches (ppc64le) build drgn from source — its vendored libdrgn uses autotools, so
# `uv sync --group live` fails at `autoreconf` without autoconf/automake/libtool. libtool the
# .deb ships `libtoolize` (no `libtool` binary); the actual libtool script is generated per
# project, so probe `libtoolize` — its presence is what drgn's build actually needs.
if arch_needs_rust "${host_arch}"; then
  for cmd in autoconf automake libtoolize; do
    require_command future "${cmd}" "${distro}"
  done
fi

report_tier "Required dependencies" required "${distro}"
report_tier "Recommended dependencies (full local CI)" recommended "${distro}"
report_tier "Future dependencies (live_vm / kernel build)" future "${distro}"

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
