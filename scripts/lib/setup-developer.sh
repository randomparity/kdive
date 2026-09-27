# shellcheck shell=bash
# Globals and dependency probes are supplied by check-setup-deps.sh.
# shellcheck disable=SC2154
# Developer dependency installation, sourced by check-setup-deps.sh --install-developer.
# Reporting and provider-host preparation remain separate from this explicit installer.

dev_install_packages() {
  (($#)) || return 0
  case "${distro}" in
  debian)
    # Reuse the bounded apt transport instead of introducing another retry policy.
    run_privileged true
    KDIVE_APT_TIMEOUT_S=300 "${setup_scripts}/apt-install.sh" "$@"
    ;;
  fedora | el) run_privileged dnf install -y "$@" ;;
  arch) run_privileged pacman -S --needed --noconfirm "$@" ;;
  opensuse) run_privileged zypper --non-interactive install "$@" ;;
  *)
    printf 'Developer setup cannot install packages on this distribution: %s\n' "${distro_exact}" >&2
    return 1
    ;;
  esac
}

dev_package_for() {
  case "$1:${distro}" in
  go:debian) printf golang-go ;;
  go:fedora | go:el | go:opensuse) printf golang ;;
  go:*) printf go ;;
  cc:*) printf gcc ;;
  xz:debian) printf xz-utils ;;
  ss:debian) printf iproute2 ;;
  ss:fedora | ss:el) printf iproute ;;
  ss:*) printf iproute2 ;;
  libseccomp:debian) printf libseccomp-dev ;;
  libseccomp:arch) printf libseccomp ;;
  libseccomp:*) printf libseccomp-devel ;;
  openssl:debian) printf libssl-dev ;;
  openssl:arch) printf openssl ;;
  openssl:*) printf openssl-devel ;;
  zlib:debian) printf zlib1g-dev ;;
  zlib:arch) printf zlib ;;
  zlib:*) printf zlib-devel ;;
  compose:debian) printf docker-compose-v2 ;;
  compose:*) printf docker-compose ;;
  rustc:fedora | rustc:el) printf rust ;;
  cabal:*) printf cabal-install ;;
  *) package_for "$1" "${distro}" ;;
  esac
}

# Download a pinned release, verify its committed digest, and install only its named binary.
dev_install_archive() (
  set -euo pipefail
  local tool="$1" url="$2" digest="$3" member="$4" temporary
  temporary="$(mktemp -d)"
  trap 'rm -rf "${temporary}"' EXIT
  curl --proto '=https' --tlsv1.2 --fail --location --retry 3 --connect-timeout 15 \
    --max-time 300 --output "${temporary}/archive" "${url}"
  printf '%s  %s\n' "${digest}" "${temporary}/archive" | sha256sum --check --strict
  tar -xf "${temporary}/archive" -C "${temporary}" "${member}"
  install -m 0755 "${temporary}/${member}" "${dev_bin}/${tool}"
)

dev_install_go_tool() {
  local tool="$1" module="$2" version="$3" selected
  selected="$(command -v "${tool}" || true)"
  if [[ -n "${selected}" ]] && go version -m "${selected}" | grep -Fq "${version}"; then
    return
  fi
  GOBIN="${dev_bin}" GOTOOLCHAIN=auto go install "${module}@${version}"
  hash -r
  selected="$(command -v "${tool}" || true)"
  if [[ -z "${selected}" ]] || ! go version -m "${selected}" | grep -Fq "${version}"; then
    printf 'Put %s before other tool directories on PATH, then rerun just setup (%s is shadowed).\n' "${dev_bin}" "${tool}" >&2
    return 1
  fi
}

install_developer_dependencies() {
  [[ "$(uname -s)" == Linux ]] || {
    printf 'Complete developer setup needs a Linux host or Linux development VM; check-deps remains available for other hosts.\n' >&2
    return 1
  }
  command_exists uv || {
    printf 'Install the bootstrap runner uv, then rerun just setup.\n' >&2
    return 1
  }
  local dev_bin setup_scripts command module shellcheck_digest prometheus_arch prometheus_digest
  local -a packages=()
  dev_bin="$(uv tool dir --bin)"
  setup_scripts="${BASH_SOURCE[0]%/lib/setup-developer.sh}"
  # Hooks run outside just too: installing to a directory absent from the caller's PATH
  # would make setup appear to work while leaving git commit/push broken.
  case ":${PATH}:" in
  *":${dev_bin}:"*) ;;
  *)
    printf 'Add %s to PATH, then rerun just setup.\n' "${dev_bin}" >&2
    return 1
    ;;
  esac
  mkdir -p "${dev_bin}"
  probe_all
  packages=("${required_packages[@]}" "${recommended_packages[@]}")
  for command in cc curl tar xz unzip make go zsh gdb tcpdump ss; do
    command_exists "${command}" || packages+=("$(dev_package_for "${command}")")
  done
  for module in libseccomp libelf; do
    pkg-config --exists "${module}" 2>/dev/null || packages+=("$(dev_package_for "${module/libelf/libelf-headers}")")
  done
  realpath -m --relative-to=/ /x >/dev/null 2>&1 && stat -c %n / >/dev/null 2>&1 || packages+=(coreutils)
  find / -maxdepth 0 -printf '' >/dev/null 2>&1 || packages+=(findutils)
  grep -qP x <<<x >/dev/null 2>&1 || packages+=(grep)
  if arch_needs_rust "${host_arch}"; then
    for command in rustc cargo; do
      command_exists "${command}" || packages+=("$(dev_package_for "${command}")")
    done
    for module in openssl zlib; do
      pkg-config --exists "${module}" 2>/dev/null || packages+=("$(dev_package_for "${module}")")
    done
  fi
  # A working externally configured daemon is respected. Never replace an existing engine.
  if ! docker compose version >/dev/null 2>&1; then
    packages+=("$(dev_package_for compose)")
  fi
  if [[ "${distro}" == fedora ]] && ! command_exists docker; then
    packages+=(docker-cli)
  fi
  dev_install_packages "${packages[@]}"
  hash -r
  uv tool install 'prek==0.4.3'
  if ! shellcheck --version 2>/dev/null | grep -Fq 'version: 0.11.0'; then
    case "${host_arch}" in
    x86_64) shellcheck_digest=8c3be12b05d5c177a04c29e3c78ce89ac86f1595681cab149b65b97c4e227198 ;;
    aarch64) shellcheck_digest=12b331c1d2db6b9eb13cfca64306b1b157a86eb69db83023e261eaa7e7c14588 ;;
    *) shellcheck_digest='' ;;
    esac
    if [[ -n "${shellcheck_digest}" ]]; then
      dev_install_archive shellcheck \
        "https://github.com/koalaman/shellcheck/releases/download/v0.11.0/shellcheck-v0.11.0.linux.${host_arch}.tar.xz" \
        "${shellcheck_digest}" shellcheck-v0.11.0/shellcheck
    else
      # ShellCheck has no upstream POWER binary. Build natively instead of installing an
      # x86 executable or silently accepting an older distro linter than CI uses.
      packages=()
      command_exists ghc || packages+=(ghc)
      command_exists cabal || packages+=(cabal-install)
      dev_install_packages "${packages[@]}"
      cabal update
      cabal install ShellCheck-0.11.0 --installdir="${dev_bin}" --install-method=copy --overwrite-policy=always
    fi
  fi
  dev_install_go_tool shfmt mvdan.cc/sh/v3/cmd/shfmt v3.13.1
  dev_install_go_tool actionlint github.com/rhysd/actionlint/cmd/actionlint v1.7.12
  dev_install_go_tool helm helm.sh/helm/v3/cmd/helm v3.21.0
  dev_install_go_tool gitleaks github.com/zricethezav/gitleaks/v8 v8.30.1
  if ! command_exists promtool; then
    case "${host_arch}" in
    x86_64)
      prometheus_arch=amd64
      prometheus_digest=20da47f8e5303f74aecb78edd7f7e39041dac08ac4939dba75efd7a900ae8867
      ;;
    aarch64)
      prometheus_arch=arm64
      prometheus_digest=281492bf04ed171cb09d24377e9777f56e55ccb6445ef197b66bd1693bd9b7f1
      ;;
    ppc64le)
      prometheus_arch=ppc64le
      prometheus_digest=2e5d4bf217e973c813922491abe665a7897578e80260fd9393fddbf20e3fa363
      ;;
    *)
      printf 'No promtool release configured for %s\n' "${host_arch}" >&2
      return 1
      ;;
    esac
    dev_install_archive promtool \
      "https://github.com/prometheus/prometheus/releases/download/v3.12.0/prometheus-3.12.0.linux-${prometheus_arch}.tar.gz" \
      "${prometheus_digest}" "prometheus-3.12.0.linux-${prometheus_arch}/promtool"
  fi
  hash -r
  shellcheck --version | grep -F 'version: 0.11.0'
  docker compose version
  if ! docker info >/dev/null 2>&1; then
    printf 'Docker is installed but unavailable to this session. Start your daemon and grant this user access (then start a new login session), or select a reachable Docker context; rerun just setup.\n' >&2
    return 1
  fi
}
