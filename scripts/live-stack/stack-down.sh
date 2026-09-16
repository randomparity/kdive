#!/usr/bin/env bash
#
# Tear down the local kdive infrastructure: stop host processes + compose backends.
# Plain teardown keeps state (the compose data volumes + any running kdive-* domains).
# `--wipe` is a full reset: it drops the compose data volumes -- kdive-pgdata,
# kdive-seaweedfs-data, kdive-build, kdive-install (ADR-0552) -- AND reaps
# kdive-provisioned libvirt domains and their qcow2 overlays (these live outside compose,
# so a DB wipe alone would orphan them).
# The prior kdive-minio-data volume is intentionally preserved: SeaweedFS never reuses it.
# libvirt itself is left enabled and running (host service; not cycled per teardown).
#
# Usage:
#   scripts/live-stack/stack-down.sh            stop the stack, keep state
#   scripts/live-stack/stack-down.sh --force    SIGKILL daemons remaining after the grace period
#   scripts/live-stack/stack-down.sh --wipe     also wipe DB + reap kdive domains/overlays
#   scripts/live-stack/stack-down.sh --wipe --yes   skip the confirmation prompt
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# Teardown needs libvirt only for --wipe's reap, and this is a tool an operator reaches for
# precisely when the host is broken — so it declares itself libvirt-free and lets a broken
# published contract degrade rather than abort at source time (ADR-0659). EXPORTED, because the
# `worker-lifecycle.sh stop` below is a child process that sources lib.sh and env.sh itself: a
# shell-local declaration would not reach it, and teardown would exit having stopped nothing.
# `stop` needs no libvirt; `start` resolves through load_published_libvirt_uri, which ignores this
# declaration and still fails closed.
export LIBVIRT_OPTIONAL=1
# shellcheck source=scripts/live-stack/lib.sh
source "${here}/lib.sh"
cd "$repo_root"

wipe=0
assume_yes=0
force=0
for arg in "$@"; do
  case "$arg" in
  --wipe) wipe=1 ;;
  --yes) assume_yes=1 ;;
  --force) force=1 ;;
  *)
    echo "unknown argument: $arg (accepts --force, --wipe, --yes)" >&2
    exit 2
    ;;
  esac
done

# --wipe is the one operation here that needs the endpoint, so it is refused up front rather than
# after the teardown has begun. Half a wipe is worse than none: the header above pairs the volume
# drop with the domain reap because the domains and overlays live outside compose, so dropping the
# volumes while unable to reach libvirt orphans exactly what the pairing exists to prevent.
if [[ "$wipe" == "1" ]]; then
  require_libvirt_uri "reap kdive domains for --wipe" || {
    # The shared message offers only endpoint repair, and both of its routes need the operator to
    # know which URI their host publishes -- the fact the broken contract just made unreadable.
    # Only this caller knows a useful partial operation exists, so only it can name the third way.
    echo "to stop the stack without reaping, re-run without --wipe" >&2
    exit 1
  }
fi

if [[ "$wipe" == "1" && "$assume_yes" != "1" ]]; then
  echo "WARNING: --wipe drops the compose data volumes (database, artifacts bucket," >&2
  echo "build/install caches) and destroys all kdive-* libvirt domains" >&2
  echo "and their overlay disks. This is irreversible." >&2
  # An interactive prompt needs a tty; under the agent `!` prefix (or any piped stdin) `read`
  # gets EOF and would silently abort. Require --yes instead of hanging/aborting confusingly.
  if [[ ! -t 0 ]]; then
    echo "non-interactive stdin: re-run as 'stack-down.sh --wipe --yes' to confirm" >&2
    exit 1
  fi
  read -r -p "Type 'wipe' to proceed: " confirm
  [[ "$confirm" == "wipe" ]] || {
    echo "aborted"
    exit 1
  }
fi

echo "=== stopping host processes ==="
if ! "${here}/worker-lifecycle.sh" stop; then
  if [[ "$force" != "1" ]]; then
    echo "worker lifecycle stop left unresolved evidence; backends remain up" >&2
    echo "restore the failed dependency and retry, or use --force to accept stranded fences" >&2
    exit 1
  fi
  echo "WARNING: --force cannot publish worker termination evidence and may strand fences." >&2
fi
stop_daemons
if [[ "$force" == "1" ]]; then
  echo "=== force-stopping host processes still running ==="
  force_stop_daemons
fi

echo "=== stopping compose backends + obs ==="
if [[ "$wipe" == "1" ]]; then
  docker compose --profile obs down -v
else
  docker compose --profile obs down
fi

if [[ "$wipe" == "1" ]]; then
  echo "=== reaping kdive-* libvirt domains + overlays ==="
  # Every call here used to end in `|| true` and the block printed one `destroying <domain>` line
  # per name it INTENDED to reach, so a reap that removed nothing still reported success (#2515).
  # An operator told the host was wiped then starts the next run from a state nobody expects.
  # What survives the rewrite is the suppression on `destroy` alone: a domain that is already shut
  # off answers non-zero and that is not a reap failure. `undefine` is the removal, so its status
  # is the verdict, and the lines below are written AFTER a removal rather than before an attempt.
  reaped=()
  unreaped=()
  while read -r dom; do
    [[ -n "$dom" ]] || continue
    sudo virsh -c "$KDIVE_LIBVIRT_URI" destroy "$dom" >/dev/null 2>&1 || true
    # `2>&1 >/dev/null` in that order captures stderr only: fd2 goes to the substitution, then fd1
    # to /dev/null. Verbatim, because which refusal it was decides the operator's next move.
    if err="$(sudo virsh -c "$KDIVE_LIBVIRT_URI" undefine "$dom" 2>&1 >/dev/null)"; then
      reaped+=("domain ${dom}")
    else
      unreaped+=("domain ${dom}: ${err:-undefine failed and said nothing}")
    fi
  done < <(kdive_domains)

  # The overlay glob is the SHELL's, expanded with the caller's own privilege — so on an account
  # that cannot list the directory it expands to nothing, which is byte-identical to a host that
  # has no overlays. That is the one place a removal failure cannot surface the no-op, because no
  # removal is ever attempted; only the listability test below tells the two apart.
  if [[ ! -d "$KDIVE_ROOTFS_DIR" ]]; then
    echo "  no overlay directory at ${KDIVE_ROOTFS_DIR}"
  elif [[ ! -r "$KDIVE_ROOTFS_DIR" || ! -x "$KDIVE_ROOTFS_DIR" ]]; then
    unreaped+=("overlays in ${KDIVE_ROOTFS_DIR}: not listable as $(id -un), so an empty directory and an unreadable one cannot be told apart")
  else
    shopt -s nullglob
    for overlay in "${KDIVE_ROOTFS_DIR}"/*-overlay.qcow2; do
      if err="$(sudo rm -f "$overlay" 2>&1 >/dev/null)"; then
        reaped+=("overlay ${overlay}")
      else
        unreaped+=("overlay ${overlay}: ${err:-rm failed and said nothing}")
      fi
    done
    shopt -u nullglob
  fi

  for item in "${reaped[@]}"; do
    echo "  removed ${item}"
  done
  echo "reaped ${#reaped[@]} item(s)"
  if ((${#unreaped[@]})); then
    echo "ERROR: --wipe did not reap the host; ${#unreaped[@]} item(s) remain:" >&2
    printf '  %s\n' "${unreaped[@]}" >&2
    exit 1
  fi
fi

echo "done"
