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

# kdive domain names on stdout, one per line, with virsh's status preserved -- and, on failure,
# virsh's diagnostic printed instead of the names. lib.sh's kdive_domains() discards stderr and
# ends in `|| true`, so an endpoint that RESOLVES but does not answer (a daemon that is down, or
# the wrong-daemon URI libvirt-uri.sh:119-121 warns about) enumerates byte-identically to a host
# holding no kdive domains -- and `require_libvirt_uri` above proved only that the contract
# resolved, never that anything answers it. The probe supplies the status that `|| true` swallows;
# kdive_domains still supplies the names, so what counts as a kdive domain stays defined once.
# Bare virsh, matching kdive_domains: which privilege the reap should hold is #2516's question.
enumerate_kdive_domains() {
  local probe_err
  probe_err="$(virsh -c "$KDIVE_LIBVIRT_URI" list --all --name 2>&1 >/dev/null)" || {
    printf '%s' "${probe_err:-virsh list failed and said nothing}"
    return 1
  }
  kdive_domains
}

if [[ "$wipe" == "1" ]]; then
  echo "=== reaping kdive-* libvirt domains + overlays ==="
  # Every call here used to end in `|| true` and the block printed one `destroying <domain>` line
  # per name it INTENDED to reach, then `done` regardless -- so a reap that removed nothing still
  # reported success (#2515) and the next run started from a state nobody expects. Every line below
  # is written from an OBSERVED end state instead of from an attempt.
  reaped=()
  unreaped=()
  domains=()
  declare -A undefine_err=()
  if ! listing="$(enumerate_kdive_domains)"; then
    unreaped+=("kdive domains: cannot enumerate at ${KDIVE_LIBVIRT_URI}, so an empty list is not evidence of an empty host -- ${listing}")
  elif [[ -n "$listing" ]]; then
    mapfile -t domains <<<"$listing"
    for dom in "${domains[@]}"; do
      # `destroy` keeps its suppression: a domain already shut off answers non-zero and that is not
      # a reap failure. `2>&1 >/dev/null` in that order captures stderr only -- fd2 to the
      # substitution, then fd1 to /dev/null -- and it is kept verbatim, because which refusal this
      # was decides the operator's next move.
      sudo virsh -c "$KDIVE_LIBVIRT_URI" destroy "$dom" >/dev/null 2>&1 || true
      undefine_err["$dom"]="$(sudo virsh -c "$KDIVE_LIBVIRT_URI" undefine "$dom" 2>&1 >/dev/null || true)"
    done
    # Neither call's status is the verdict. `virsh undefine` on a RUNNING domain succeeds by
    # converting it to a transient one WITHOUT stopping it, so an undefine that returned 0 after a
    # destroy the host refused would read as a removal while the guest is still up. Re-reading the
    # list settles that and every other residue in one call.
    if ! end_state="$(enumerate_kdive_domains)"; then
      still_there="unverifiable: the end state could not be re-read -- ${end_state}"
      end_state="$listing"
    else
      still_there="still defined after destroy + undefine"
    fi
    for dom in "${domains[@]}"; do
      if [[ $'\n'"${end_state}"$'\n' == *$'\n'"${dom}"$'\n'* ]]; then
        unreaped+=("domain ${dom}: ${still_there}${undefine_err[$dom]:+ -- ${undefine_err[$dom]}}")
      else
        reaped+=("domain ${dom}")
      fi
    done
  fi

  # Half a wipe is worse than none, the same pairing the header and the --wipe gate above keep: a
  # domain that survived still needs its backing overlay, so an incomplete domain reap stops here
  # rather than deleting the disks out from under it.
  if ((${#unreaped[@]})); then
    echo "  skipping overlays: the domain reap did not complete" >&2
  elif [[ ! -d "$KDIVE_ROOTFS_DIR" ]]; then
    echo "  no overlay directory at ${KDIVE_ROOTFS_DIR}"
  elif [[ ! -r "$KDIVE_ROOTFS_DIR" || ! -x "$KDIVE_ROOTFS_DIR" ]]; then
    # The overlay glob is the CALLING SHELL's, expanded with the caller's own privilege, while the
    # removal below runs under sudo. On an account outside the directory's owner and group it
    # expands to nothing, so a host full of overlays is byte-identical to a clean one -- the one
    # place a removal failure cannot surface the no-op, because no removal is ever attempted.
    unreaped+=("overlays in ${KDIVE_ROOTFS_DIR}: not listable as $(id -un), so an empty directory and an unreadable one cannot be told apart; re-run as the account that owns it or one in its group (ls -ld names them)")
  else
    shopt -s nullglob
    for overlay in "${KDIVE_ROOTFS_DIR}"/*-overlay.qcow2; do
      if rm_err="$(sudo rm -f "$overlay" 2>&1 >/dev/null)"; then
        reaped+=("overlay ${overlay}")
      else
        unreaped+=("overlay ${overlay}: ${rm_err:-rm failed and said nothing}")
      fi
    done
    shopt -u nullglob
  fi

  # Guarded, not bare: expanding an empty array under `set -u` is an error before bash 4.4, and
  # nothing in this script otherwise needs a bash newer than the 4.0 `mapfile` above.
  if ((${#reaped[@]})); then
    printf '  removed %s\n' "${reaped[@]}"
  fi
  echo "reaped ${#reaped[@]} item(s)"
  if ((${#unreaped[@]})); then
    echo "ERROR: --wipe did not reap the host; ${#unreaped[@]} item(s) remain:" >&2
    printf '  %s\n' "${unreaped[@]}" >&2
    exit 1
  fi
fi

echo "done"
