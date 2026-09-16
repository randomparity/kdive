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

# kdive domain names on stdout, one per line, with virsh's status preserved -- and, on failure,
# virsh's diagnostic printed instead of the names. This is the tree's one definition of the
# `^kdive-` domain predicate: lib.sh's `kdive_domains()` discarded virsh's stderr and status and
# ended in `|| true`, making an endpoint that RESOLVES but does not answer enumerate
# byte-identically to a host holding no kdive domains, so it was removed as dead code (#2559)
# rather than kept in step with this one. `require_libvirt_uri` below proves only that the
# contract resolved, never that anything answers it.
#
# What this distinguishes is an endpoint that answers from one that does not, and NOTHING MORE. It
# does not cover the wrong-daemon URI libvirt-uri.sh's resolve_libvirt_uri comment records: a
# daemon that is running but holds no kdive domains answers with exit 0 and no output, which is why
# every zero-domain report below names the endpoint it consulted rather than calling the host clean.
# Cited by name, not by line: that passage has already moved once.
#
# ONE virsh call, not a status probe plus a separate name read: a daemon lost between two calls
# would return an empty list with exit 0, which is the silent no-op this function exists to refuse,
# reached through the gap between the probe and the data.
#
# Routed through reap_run, not bare. This used to enumerate as the invoking account while destroy
# and undefine escalated, so for an explicit per-identity endpoint (`qemu:///session`,
# `qemu+ssh://`) the list that GRADES the reap could come from a different daemon than the one the
# removal MUTATED -- #2515 recorded the reporting consequence and left the privilege to #2516.
# ADR-0662 settles it: ONE privilege, derived from the endpoint, for observation and mutation
# alike, so the two can no longer disagree.
enumerate_kdive_domains() {
  local out
  out="$(reap_run virsh -c "$KDIVE_LIBVIRT_URI" list --all --name 2>&1)" || {
    printf '%s' "${out:-virsh list failed and said nothing}"
    return 1
  }
  grep -E '^kdive-' <<<"$out" || true
}

# ADR-0662: the --wipe reap's privilege follows the endpoint it was published. reap_as_root is set
# in the --wipe branch below and DELIBERATELY nowhere else -- under `set -u` a call from outside
# that branch dies with `reap_as_root: unbound variable` rather than silently picking a privilege.
reap_run() {
  if ((reap_as_root)); then
    sudo "$@"
  else
    "$@"
  fi
}

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
  # ADR-0662. resolve_libvirt_uri publishes two daemon scopes to one variable: the lifecycle
  # contract's operator-owned session endpoint, and the qemu:///system fallback on a host without
  # that contract, which is root's. One fixed privilege cannot be right for both.
  #
  # `sudo` against the session socket BYPASSES the ownership gate rather than satisfying it -- the
  # installer writes that socket `operator_uid:group_gid:770` and /var/lib/kdive/rootfs
  # `operator:kdive-live-libvirt` 2770, and root owns neither -- while against a plain
  # `qemu:///session` it reaches root's own per-uid daemon, which is a different host's worth of
  # domains. So: `/session` in the path is reaped as the invoking account, anything else keeps sudo.
  #
  # The query is stripped because the published URIs carry the socket path there, not the scope.
  # Classification is textual and an unclassifiable value falls to the escalating branch, so the
  # failure direction is today's behaviour rather than a silent loss of privilege.
  #
  # HERE, not at the top of the file: KDIVE_LIBVIRT_URI may be unset in the LIBVIRT_OPTIONAL
  # degraded state, and a top-level `case` would kill a plain teardown on a broken contract under
  # `set -u`. This point is past require_libvirt_uri, so the endpoint is in hand.
  case "${KDIVE_LIBVIRT_URI%%\?*}" in
  */session) reap_as_root=0 ;;
  *) reap_as_root=1 ;;
  esac
  # Liveness, and it belongs HERE rather than at the reap. require_libvirt_uri proves the contract
  # resolved; a daemon that is stopped behind a perfectly valid contract passes it. Discovering
  # that at the reap means `docker compose --profile obs down -v` has already run, so the volumes
  # are gone and the domains are not -- the half-wipe this gate's own comment exists to prevent,
  # and the shape ADR-0659 prescribes refusing wholesale instead. Enumeration is read-only, so it
  # is safe before anything is stopped. The reap enumerates again regardless: this proves liveness
  # at gate time, not at reap time, and a daemon lost in between still lands in `unreaped` there.
  gate_out="$(enumerate_kdive_domains)" || {
    echo "cannot reach ${KDIVE_LIBVIRT_URI} to reap kdive domains for --wipe:" >&2
    echo "  ${gate_out}" >&2
    echo "nothing has been stopped or dropped; restore the endpoint and retry," >&2
    echo "or stop the stack without reaping by re-running without --wipe" >&2
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
  echo "=== reaping kdive-* libvirt domains + overlays at ${KDIVE_LIBVIRT_URI} ==="
  # Every call here used to end in `|| true` and the block printed one `destroying <domain>` line
  # per name it INTENDED to reach, then `done` regardless -- so a reap that removed nothing still
  # reported success (#2515) and the next run started from a state nobody expects. Every line below
  # is written from an OBSERVED end state instead of from an attempt.
  reaped=()
  unreaped=()
  zero_domains=0
  declare -A undefine_err=()
  if ! listing="$(enumerate_kdive_domains)"; then
    unreaped+=("kdive domains: cannot enumerate at ${KDIVE_LIBVIRT_URI}, so an empty list is not evidence of an empty host -- ${listing}")
  elif [[ -z "$listing" ]]; then
    # Named, never bare: an endpoint that answers with nothing is either a clean host or the
    # wrong daemon of the two libvirt-uri.sh publishes. Recorded, because the overlay sweep below
    # is about to see the other half of the only evidence that separates them.
    echo "  no kdive domains at ${KDIVE_LIBVIRT_URI}"
    zero_domains=1
  else
    mapfile -t domains <<<"$listing"
    for dom in "${domains[@]}"; do
      # `destroy` keeps its suppression: a domain already shut off answers non-zero and that is not
      # a reap failure. `2>&1 >/dev/null` in that order captures stderr only -- fd2 to the
      # substitution, then fd1 to /dev/null -- and it is kept verbatim, because which refusal this
      # was decides the operator's next move.
      reap_run virsh -c "$KDIVE_LIBVIRT_URI" destroy "$dom" >/dev/null 2>&1 || true
      undefine_err["$dom"]="$(reap_run virsh -c "$KDIVE_LIBVIRT_URI" undefine "$dom" 2>&1 >/dev/null || true)"
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
    # The overlay glob is the CALLING SHELL's, expanded with the caller's own privilege, and on the
    # escalating branch the removal below does not share it. On an account outside the directory's
    # owner and group the glob expands to nothing, so a host full of overlays is byte-identical to
    # a clean one -- the one place a removal failure cannot surface the no-op, because no removal
    # is ever attempted. Hence this refusal rather than an empty sweep. On the session branch the
    # asymmetry is gone (ADR-0662), but the refusal stays: it is what makes the escalating branch
    # safe, and an operator who cannot list the directory has the same problem either way.
    unreaped+=("overlays in ${KDIVE_ROOTFS_DIR}: not listable as $(id -un), so an empty directory \
and an unreadable one cannot be told apart; re-run as the account that owns it or one in its \
group (ls -ld names them)")
  elif ((!reap_as_root)) && [[ ! -w "$KDIVE_ROOTFS_DIR" ]]; then
    # ADR-0662 put the overlay `rm` on the invoking account for a session endpoint, so the
    # directory has to be WRITABLE and not merely listable. The test above was written when the
    # removal was root's and could not be refused; unlinking needs write on the DIRECTORY, which
    # `-r`/`-x` never covered. Reachable without any exotic host: stack-services.sh:232 does
    # `sudo install -d -o "$(id -un)" -m 0755` on this same path, so an account other than the
    # operator running bring-up leaves the installer's `operator:kdive-live-libvirt` 2770 as a
    # 0755 directory the operator can no longer write.
    #
    # Refused once here rather than per overlay: every `rm` would fail identically, and this names
    # the fix instead of repeating one permission error per disk. Guarded on the branch, because
    # on the escalating branch root's `rm` does not need it and a root-owned 0755 directory is the
    # normal bare-host shape -- testing `-w` unconditionally would refuse that working host.
    unreaped+=("overlays in ${KDIVE_ROOTFS_DIR}: not writable as $(id -un), and a session \
endpoint's overlays are removed as the invoking account (ADR-0662), so every rm would be refused; \
re-run as the account that owns it or one in its group (ls -ld names them)")
  else
    shopt -s nullglob
    overlays_removed=0
    for overlay in "${KDIVE_ROOTFS_DIR}"/*-overlay.qcow2; do
      # Graded on the end state, like the domain half above and for the same reason: `rm`'s exit
      # status is what it attempted, and the line this block prints claims what is gone.
      rm_err="$(reap_run rm -f "$overlay" 2>&1 >/dev/null || true)"
      if [[ ! -e "$overlay" ]]; then
        reaped+=("overlay ${overlay}")
        overlays_removed=$((overlays_removed + 1))
      else
        unreaped+=("overlay ${overlay}: still present after rm${rm_err:+ -- ${rm_err}}")
      fi
    done
    shopt -u nullglob
    # The one local contradiction available: the endpoint reported no kdive domains, and this
    # directory held their backing disks. A host cleaned in a previous pass looks the same, which
    # is why this warns instead of refusing -- sweeping genuinely orphaned overlays is a purpose
    # of --wipe, and refusing them would trade a silent wrong outcome for a loud one. But a
    # wrong-daemon URI (libvirt-uri.sh's resolve_libvirt_uri comment, which its own repair guidance
    # can hand an operator) lands here too, and there the domains are alive on another daemon and
    # have just lost their disks. Saying so keeps `no kdive domains` from reading as "nothing to do".
    if ((zero_domains && overlays_removed)); then
      echo "WARNING: removed ${overlays_removed} overlay(s) while ${KDIVE_LIBVIRT_URI} reported zero" >&2
      echo "kdive domains. If the domains live on another daemon they are now without their disks." >&2
    fi
  fi

  # Guarded, not bare: expanding an empty array under `set -u` is an error before bash 4.4, and
  # nothing in this script otherwise needs a bash newer than the 4.0 `mapfile` above.
  if ((${#reaped[@]})); then
    printf '  removed %s\n' "${reaped[@]}"
  fi
  echo "reaped ${#reaped[@]} item(s) at ${KDIVE_LIBVIRT_URI}"
  if ((${#unreaped[@]})); then
    echo "ERROR: --wipe did not reap the host; ${#unreaped[@]} item(s) remain:" >&2
    printf '  %s\n' "${unreaped[@]}" >&2
    # The volume drop is irreversible and it already ran, thirty lines above: every failure this
    # block can report is discovered after it. An operator reading only the list would infer the
    # database survived, so the state the run has already reached is stated rather than implied.
    echo "the compose data volumes were already dropped before this reap ran; the stack brings up" >&2
    echo "on an empty database once the items above are dealt with, or re-run without --wipe" >&2
    exit 1
  fi
fi

echo "done"
