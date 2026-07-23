#!/usr/bin/env bash
# Refresh the self-hosted live-vm warm store: a bootable rootfs + its kernel + matching vmlinux
# debuginfo, kept warm (NVR-pinned) between nightly runs (ADR-0388). Emits the eval-safe
# KDIVE_LIVE_VM_* wiring block on stdout; human progress goes to stderr.
set -euo pipefail
# Bash disables errexit INSIDE `$(...)`, and the builders below are captured that way
# (`build_id="$(produce_rootfs_and_kernel ...)"`). Without inherit_errexit a failing virt-copy-out
# or mv is swallowed and the refresh runs on to report a misleading downstream fault (an absent
# vmlinux reads as "needs extract-vmlinux") instead of the real one. Needs bash 4.4+, which every
# Linux host this script targets has; it is unusable on macOS's bash 3.2 regardless (libguestfs).
shopt -s inherit_errexit

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/live-vm/lib.sh
source "${here}/lib.sh"

STORE="${KDIVE_WARM_STORE_DIR:-/var/lib/kdive/warm-store}"
# Supplied pins (the operator/D compute the NVR from the base image; no live distro query here).
TARGET="${KDIVE_WARM_STORE_TARGET_NVR:?set KDIVE_WARM_STORE_TARGET_NVR to the pinned kernel NVR}"
IMAGE="${KDIVE_WARM_STORE_IMAGE:?set KDIVE_WARM_STORE_IMAGE to the catalog rootfs image}"

require_tools \
  "$(kdive_python):the kdive venv (set KDIVE_PYTHON), runs build-fs" \
  "virt-ls:libguestfs-tools" "virt-copy-out:libguestfs-tools" \
  "eu-readelf:elfutils" "debuginfod-find:debuginfod"
require_kdive_module

mkdir -p -- "$STORE"

# Serialize refreshes; the consuming boot (sub-issue D) takes a shared lock on the same file.
exec 9>"${STORE}/.lock"
flock 9

is_warm() {
  local cur="${STORE}/current" m="${STORE}/current/MANIFEST"
  [ "${KDIVE_WARM_STORE_FORCE:-0}" = "1" ] && return 1
  [ -e "$m" ] || return 1
  store_manifest_matches "$m" "$TARGET" || return 1
  # Non-fatal digest re-checks: a corrupt-but-present file makes this false -> rebuild, never die.
  sha256_ok "${cur}/rootfs.qcow2" "$(manifest_field "$m" rootfs_sha256)" &&
    sha256_ok "${cur}/vmlinux" "$(manifest_field "$m" kernel_sha256)" &&
    sha256_ok "${cur}/vmlinux.debug" "$(manifest_field "$m" debuginfo_sha256)"
}

main() {
  local new build_id kver
  # Fail fast on a fetch-infra misconfig BEFORE the minutes-long, multi-GB build (else a
  # misconfigured nightly builds a rootfs only to discard it when debuginfod-find dies).
  [ -n "${DEBUGINFOD_URLS:-}" ] ||
    die "debuginfod fetch infra not configured: set DEBUGINFOD_URLS to a server indexing the guest kernel debuginfo"
  prune_other_sets "$STORE" # entry sweep: reclaim any crashed refresh's orphan set dirs.

  if is_warm; then
    report_usage "warm-store" "$STORE"
    emit_wiring "${STORE}/current"
    return 0
  fi

  new="$(mktemp -d -- "${STORE}/set-XXXXXX")"
  # shellcheck disable=SC2064  # expand $new now so the trap cleans this exact dir on any pre-commit exit.
  trap "rm -rf -- '$new'" EXIT

  build_id="$(produce_rootfs_and_kernel "$new" "$IMAGE")"
  # Tie the supplied pin to the artifact: the built kernel's uname release must appear in TARGET,
  # catching a KDIVE_WARM_STORE_IMAGE/pin mismatch rather than silently mislabelling the manifest.
  kver="$(cat "${new}/.kver")"
  case "$TARGET" in
  *"$kver"*) ;;
  *) die "pinned NVR '${TARGET}' does not contain the built kernel version '${kver}' (image/pin mismatch?)" ;;
  esac
  rm -f -- "${new}/.kver"

  fetch_debuginfo "$new" "$build_id"

  write_manifest "${new}/MANIFEST" "$TARGET" "$build_id" \
    "$(sha256_of "${new}/rootfs.qcow2")" "$(sha256_of "${new}/vmlinux")" \
    "$(sha256_of "${new}/vmlinux.debug")"

  commit_set "$STORE" "$new"
  trap - EXIT # committed: the set is now live, do not remove it.
  prune_other_sets "$STORE"
  report_usage "warm-store" "$STORE"
  emit_wiring "${STORE}/current"
}

main "$@"
