#!/usr/bin/env bash
# Fail-loud preflight for the live_vm CI gates (#1293, ADR-0389). Given one or more DECLARED
# families, assert each family's requirements are present and FAIL the job (never a green skip)
# when they are not. Reuses the scripts/live-vm/lib.sh die/require_* idiom.
#
# Families are env contracts (throwaway/provisioned/tcg) plus `host`, the libvirt/KVM contract the
# image BUILD needs. Declare `host` before staging, the env families after it — the env families
# assert paths that staging produces, so they cannot run first.
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/live-vm/lib.sh
source "${here}/lib.sh"

[ "$#" -ge 1 ] || die "usage: preflight-env.sh <host|throwaway|provisioned|tcg> [more...]"

# NAME: die unless the named env var is non-empty.
require_set() {
  local name="$1"
  [ -n "${!name:-}" ] || die "required env ${name} is unset for the declared family"
}

# NAME: die unless the named env var points at an existing path.
require_path() {
  local name="$1"
  require_set "$name"
  [ -e "${!name}" ] || die "${name}=${!name} does not exist"
}

# The HOST contract every family that builds an image depends on: build-fs boots a customization
# guest through libvirt (rootfs_build.resolve_accel) and drives libguestfs, so a missing daemon or
# an unusable /dev/kvm otherwise surfaces minutes into a multi-GB build as an opaque libvirt socket
# error. Deliberately checks the CONFIGURED uri rather than qemu:///system: the live_vm gates run
# session-mode (worker-owned QEMU, readable console — ADR-0223), which the system-mode probe in
# scripts/check-local-libvirt.sh cannot express.
check_host() {
  require_set KDIVE_LIBVIRT_URI
  require_tools "virsh:libvirt-clients" "qemu-img:qemu-utils"
  local kvm="${KDIVE_KVM_NODE:-/dev/kvm}"
  # Fatal, not a warning: without KVM the libguestfs appliance falls back to emulation and a gate
  # that already spends minutes building would run well past its timeout.
  { [ -r "$kvm" ] && [ -w "$kvm" ]; } ||
    die "${kvm} is not readable+writable by $(id -un): the libguestfs appliance would fall back to slow emulation (chmod 0666 ${kvm}, or join the kvm group and re-login)"
  virsh -c "$KDIVE_LIBVIRT_URI" list >/dev/null 2>&1 ||
    die "cannot reach the libvirt daemon at ${KDIVE_LIBVIRT_URI}: is libvirt installed and running (libvirtd.socket for qemu:///system; XDG_RUNTIME_DIR + linger for qemu:///session)?"
}

check_throwaway() {
  require_path KDIVE_LIVE_VM_ROOTFS
  require_set KDIVE_LIBVIRT_URI
}

check_provisioned() {
  # Teeth over A's resolver: A returns AVAILABLE on endpoint+bucket alone, so a declared family with
  # no minted System skips green. The AWS_* creds are the on-box MinIO minioadmin default (env.sh),
  # so a credential-absence check is vacuous here — the System id is the real assertion.
  require_set KDIVE_LIVE_VM_SYSTEM_ID
  require_set KDIVE_S3_ENDPOINT_URL
  require_set KDIVE_S3_BUCKET
}

check_tcg() {
  require_set KDIVE_STACK_BASE_URL
  require_set KDIVE_OIDC_ISSUER
  require_set KDIVE_DATABASE_URL
  require_set KDIVE_S3_ENDPOINT_URL
  require_set KDIVE_S3_BUCKET
  require_set AWS_ACCESS_KEY_ID # belt-and-suspenders: env.sh supplies the on-box MinIO minioadmin
  require_set AWS_SECRET_ACCESS_KEY
  require_path KDIVE_GUEST_IMAGE_PPC64LE
  require_path KDIVE_KERNEL_SRC
  # Reuse lib.sh's require_tools (already sourced) rather than an inline command -v/die.
  require_tools "qemu-system-ppc64:the ppc64le TCG guest emulator (install qemu-system-ppc)"
}

for family in "$@"; do
  case "$family" in
  host) check_host ;;
  throwaway) check_throwaway ;;
  provisioned) check_provisioned ;;
  tcg) check_tcg ;;
  *) die "unknown family '${family}' (expected host|throwaway|provisioned|tcg)" ;;
  esac
done
echo "live_vm preflight: all declared families ($*) have their required env" >&2
