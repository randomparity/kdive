#!/usr/bin/env bash
#
# Tear down the local kdive infrastructure: stop host processes + compose backends.
# Plain teardown keeps state (Postgres volume + any running kdive-* domains). `--wipe` is a
# full reset: it drops the Postgres volume AND reaps kdive-provisioned libvirt domains and
# their qcow2 overlays (these live outside compose, so a DB wipe alone would orphan them).
# libvirt itself is left enabled and running (host service; not cycled per teardown).
#
# Usage:
#   scripts/live-stack/down.sh            stop the stack, keep state
#   scripts/live-stack/down.sh --wipe     also wipe DB + reap kdive domains/overlays
#   scripts/live-stack/down.sh --wipe --yes   skip the confirmation prompt
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/live-stack/lib.sh
source "${here}/lib.sh"
cd "$repo_root"

wipe=0
assume_yes=0
for arg in "$@"; do
  case "$arg" in
  --wipe) wipe=1 ;;
  --yes) assume_yes=1 ;;
  *)
    echo "unknown argument: $arg (accepts --wipe, --yes)" >&2
    exit 2
    ;;
  esac
done

if [[ "$wipe" == "1" && "$assume_yes" != "1" ]]; then
  echo "WARNING: --wipe drops the Postgres volume and destroys all kdive-* libvirt domains" >&2
  echo "and their overlay disks. This is irreversible." >&2
  # An interactive prompt needs a tty; under the agent `!` prefix (or any piped stdin) `read`
  # gets EOF and would silently abort. Require --yes instead of hanging/aborting confusingly.
  if [[ ! -t 0 ]]; then
    echo "non-interactive stdin: re-run as 'down.sh --wipe --yes' to confirm" >&2
    exit 1
  fi
  read -r -p "Type 'wipe' to proceed: " confirm
  [[ "$confirm" == "wipe" ]] || {
    echo "aborted"
    exit 1
  }
fi

echo "=== stopping host processes ==="
stop_daemons

echo "=== stopping compose backends + obs ==="
if [[ "$wipe" == "1" ]]; then
  docker compose --profile obs down -v
else
  docker compose --profile obs down
fi

if [[ "$wipe" == "1" ]]; then
  echo "=== reaping kdive-* libvirt domains + overlays ==="
  while read -r dom; do
    [[ -n "$dom" ]] || continue
    echo "  destroying ${dom}"
    sudo virsh -c "$KDIVE_LIBVIRT_URI" destroy "$dom" 2>/dev/null || true
    sudo virsh -c "$KDIVE_LIBVIRT_URI" undefine "$dom" 2>/dev/null || true
  done < <(kdive_domains)
  sudo rm -f "${KDIVE_ROOTFS_DIR}"/*-overlay.qcow2 2>/dev/null || true
fi

echo "done"
