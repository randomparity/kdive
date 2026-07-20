#!/usr/bin/env bash
# Fail-loud env preflight for the live_vm CI gates (#1293, ADR-0389). Given one or more DECLARED
# families, assert each family's required env is present and FAIL the job (never a green skip) when
# it is not. Reuses the scripts/live-vm/lib.sh die/require_* idiom.
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/live-vm/lib.sh
source "${here}/lib.sh"

[ "$#" -ge 1 ] || die "usage: preflight-env.sh <throwaway|provisioned|tcg> [more...]"

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
  throwaway) check_throwaway ;;
  provisioned) check_provisioned ;;
  tcg) check_tcg ;;
  *) die "unknown family '${family}' (expected throwaway|provisioned|tcg)" ;;
  esac
done
echo "live_vm preflight: all declared families ($*) have their required env" >&2
