#!/usr/bin/env bash
# Refresh the self-hosted live-vm warm store: a bootable rootfs + its kernel + matching vmlinux
# debuginfo, kept warm (NVR-pinned) between nightly runs (ADR-0388). Emits the eval-safe
# KDIVE_LIVE_VM_* wiring block on stdout; human progress goes to stderr.
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/live-vm/lib.sh
source "${here}/lib.sh"

STORE="${KDIVE_WARM_STORE_DIR:-/var/lib/kdive/warm-store}"
# Supplied pins (the operator/D compute the NVR from the base image; no live distro query here).
TARGET="${KDIVE_WARM_STORE_TARGET_NVR:?set KDIVE_WARM_STORE_TARGET_NVR to the pinned kernel NVR}"
IMAGE="${KDIVE_WARM_STORE_IMAGE:?set KDIVE_WARM_STORE_IMAGE to the catalog rootfs image}"
mkdir -p -- "$STORE"

# Serialize refreshes; the consuming boot (sub-issue D) takes a shared lock on the same file.
exec 9>"${STORE}/.lock"
flock 9

manifest() { printf '%s/current/MANIFEST' "$STORE"; }

emit_wiring() {
  printf 'KDIVE_LIVE_VM_ROOTFS=%s/current/rootfs.qcow2\n' "$STORE"
  printf 'KDIVE_LIVE_VM_BZIMAGE=%s/current/vmlinux\n' "$STORE"
  printf 'KDIVE_LIVE_VM_VMLINUX=%s/current/vmlinux.debug\n' "$STORE"
}

is_warm() {
  local cur="${STORE}/current" m
  m="$(manifest)"
  [ "${KDIVE_WARM_STORE_FORCE:-0}" = "1" ] && return 1
  [ -e "$m" ] || return 1
  store_manifest_matches "$m" "$TARGET" || return 1
  # Non-fatal digest re-checks: a corrupt-but-present file makes this false -> rebuild, never die.
  sha256_ok "${cur}/rootfs.qcow2" "$(manifest_field "$m" rootfs_sha256)" &&
    sha256_ok "${cur}/vmlinux" "$(manifest_field "$m" kernel_sha256)" &&
    sha256_ok "${cur}/vmlinux.debug" "$(manifest_field "$m" debuginfo_sha256)"
}

main() {
  local new dbg build_id
  prune_other_sets "$STORE" # entry sweep: reclaim any crashed refresh's orphan set dirs.

  if is_warm; then
    report_usage "warm-store" "$STORE"
    emit_wiring
    return 0
  fi

  new="$(mktemp -d -- "${STORE}/set-XXXXXX")"
  # shellcheck disable=SC2064  # expand $new now so the trap cleans this exact dir on any pre-commit exit.
  trap "rm -rf -- '$new'" EXIT

  export DEBUGINFOD_CACHE_PATH="${new}" # pin the ~1.2 GB download onto the store's filesystem.
  build_id="$(produce_rootfs_and_kernel "$new" "$IMAGE")"
  dbg="$(debuginfod-find debuginfo "$build_id")" ||
    die "debuginfod-find failed for build-id ${build_id} (DEBUGINFOD_URLS set? kernel published?)"
  cp -- "$dbg" "${new}/vmlinux.debug"
  # REAL match: read the build-id from the FETCHED debuginfo (a bare ELF), not the kernel again.
  build_ids_match "$build_id" "$(elf_build_id "${new}/vmlinux.debug")"

  write_manifest "${new}/MANIFEST" "$TARGET" "$build_id" \
    "$(sha256_of "${new}/rootfs.qcow2")" "$(sha256_of "${new}/vmlinux")" \
    "$(sha256_of "${new}/vmlinux.debug")"

  commit_set "$STORE" "$new"
  trap - EXIT # committed: the set is now live, do not remove it.
  prune_other_sets "$STORE"
  report_usage "warm-store" "$STORE"
  emit_wiring
}

main "$@"
