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
# (e.g. `/` or `/mnt`) so a misconfigured value cannot rm -rf a mount root. Runs first — a
# dangerous path must be rejected before anything else.
case "$STAGE" in
"" | /) die "refusing to operate on '${STAGE}'" ;;
esac
[ "$mnt_root" = "/" ] && die "refusing rm -rf on the top-level path ${STAGE}; use a subdirectory"

require_tools \
  "$(kdive_python):the kdive venv (set KDIVE_PYTHON), runs build-fs" \
  "virt-ls:libguestfs-tools" "virt-copy-out:libguestfs-tools" \
  "eu-readelf:elfutils" "debuginfod-find:debuginfod"

trap 'rm -rf -- "$STAGE"' EXIT # a failed run leaves no half-populated /mnt for the next to trust.
rm -rf -- "$STAGE"
mkdir -p -- "$STAGE"
# 1. Pre-stage best-effort free-space check for the WHOLE budget (staged set + cache copy + vmcore).
require_free_space "$mnt_root" "$BUDGET" "hosted TCG image set"

# 2. Require the fetch infra BEFORE the (minutes-long) build, so a misconfig fails fast.
[ -n "${DEBUGINFOD_URLS:-}" ] ||
  die "debuginfod fetch infra not configured: set DEBUGINFOD_URLS to a server indexing ppc64le kernels"

# 3. Build the ppc64le rootfs + extract its own kernel; read the build-id from the actual artifact.
build_id="$(produce_rootfs_and_kernel "$STAGE" "$IMAGE")"
rm -f -- "${STAGE}/.kver" # produce records the pin marker; the TCG set has no NVR pin, so drop it.

# 4. Fetch the matching debuginfo by build-id (caches under the stage dir, prunes it, verifies match).
fetch_debuginfo "$STAGE" "$build_id"

# 5. Post-stage footprint cap on the staged set only.
enforce_budget "$STAGE" "$BUDGET" "hosted TCG image set"

trap - EXIT # staged successfully; keep the set.
report_usage "tcg-stage" "$STAGE"
emit_wiring "$STAGE"
