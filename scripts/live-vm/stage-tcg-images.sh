#!/usr/bin/env bash
# Stage the hosted-runner TCG image set (ppc64le rootfs + its kernel + matching debuginfo) onto the
# runner's /mnt scratch, under a measured, enforced disk budget (ADR-0388). Debuginfo is fetched on
# demand by build-id via debuginfod. Emits the eval-safe KDIVE_LIVE_VM_* wiring block on stdout.
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/live-vm/lib.sh
source "${here}/lib.sh"

STAGE="${KDIVE_TCG_STAGE_DIR:-/mnt/kdive-tcg}"
BUDGET="${KDIVE_TCG_BUDGET_BYTES:-7000000000}" # ~7 GB whole-budget ceiling (see spec disk budget).
IMAGE="${KDIVE_TCG_IMAGE:?set KDIVE_TCG_IMAGE to the ppc64le catalog rootfs image}"
mnt_root="$(dirname -- "$STAGE")"

# Floor guard before the recursive delete: refuse a root or top-level KDIVE_TCG_STAGE_DIR override
# (e.g. `/` or `/mnt`) so a misconfigured value cannot rm -rf a mount root.
case "$STAGE" in
"" | /) die "refusing to operate on '${STAGE}'" ;;
esac
[ "$mnt_root" = "/" ] && die "refusing rm -rf on the top-level path ${STAGE}; use a subdirectory"

trap 'rm -rf -- "$STAGE"' EXIT # a failed run leaves no half-populated /mnt for the next to trust.
rm -rf -- "$STAGE"
mkdir -p -- "$STAGE"
# Cache the download under the stage dir (on /mnt, the budgeted fs, not the small root fs), then
# prune it after copying so enforce_budget measures only the staged set, not a doubled cache.
export DEBUGINFOD_CACHE_PATH="${STAGE}/.dbgcache"

# 1. Pre-stage best-effort free-space check for the WHOLE budget (staged set + cache copy + vmcore).
require_free_space "$mnt_root" "$BUDGET" "hosted TCG image set"

# 2. Require the fetch infra BEFORE the (minutes-long) build, so a misconfig fails fast.
[ -n "${DEBUGINFOD_URLS:-}" ] ||
  die "debuginfod fetch infra not configured: set DEBUGINFOD_URLS to a server indexing ppc64le kernels"

# 3. Build the ppc64le rootfs + extract its own kernel; read the build-id from the actual artifact.
build_id="$(produce_rootfs_and_kernel "$STAGE" "$IMAGE")"

# 4. Fetch matching debuginfo by build-id, with three DISTINCT fail-loud outcomes.
if dbg="$(debuginfod-find debuginfo "$build_id" 2>/dev/null)"; then
  cp -- "$dbg" "${STAGE}/vmlinux.debug"
else
  rc=$?
  # debuginfod-find: exit 1 == not found (index lag); other non-zero == transient/network.
  if [ "$rc" -eq 1 ]; then
    die "debuginfo not yet published for build-id ${build_id} (distro index lag)"
  fi
  die "transient debuginfod error (rc=${rc}) fetching build-id ${build_id}; retry the run"
fi
rm -rf -- "${STAGE}/.dbgcache" "${STAGE}/.kver" # keep only the wired artifacts in the staged set.
# REAL match: read the build-id from the FETCHED debuginfo (a bare ELF), not the kernel again.
build_ids_match "$build_id" "$(elf_build_id "${STAGE}/vmlinux.debug")"

# 5. Post-stage footprint cap on the staged set only.
enforce_budget "$STAGE" "$BUDGET" "hosted TCG image set"

trap - EXIT # staged successfully; keep the set.
report_usage "tcg-stage" "$STAGE"
printf 'KDIVE_LIVE_VM_ROOTFS=%s/rootfs.qcow2\n' "$STAGE"
printf 'KDIVE_LIVE_VM_BZIMAGE=%s/vmlinux\n' "$STAGE"
printf 'KDIVE_LIVE_VM_VMLINUX=%s/vmlinux.debug\n' "$STAGE"
