#!/usr/bin/env bash
# Source-only parser and resolver for the root-published live-worker libvirt endpoint.
#
# Sourced more than once per shell by design — lib.sh, env.sh and worker-lifecycle.sh each source
# it, and examples/local-libvirt/env.sh reaches it through env.sh — so these are plain assignments.
# `readonly` aborted the second source with "readonly variable" before the body ran, which under
# the callers' `set -euo pipefail` took the whole shell down.
#
# LIBVIRT_ENV is therefore overridable. It is a test-staging seam, not an operator knob: no
# KDIVE_ prefix, no row in the generated config reference, and deliberately so — it names a path,
# not a runtime setting kdive.config reads.
#
# What bounds a redirect to a file that IS there is the allowlist in load_published_libvirt_uri:
# the content must be exactly one KDIVE_LIBVIRT_URI line naming one of the two URIs in
# LIBVIRT_SOCKET_URIS, and bash imports no array variables, so that array cannot be widened from
# the environment either. A redirect can therefore only ever yield a value that was already
# correct. The root:root 0644 probe in require_exact_libvirt_env is NOT part of that bound: it
# shells out to `stat` through the caller's PATH, so a caller who can set LIBVIRT_ENV can answer
# the probe too — which is exactly how the tests stage a contract. It guards a tampered /etc
# entry, which is meaningful because /etc/kdive is root:root 0755, not the caller who chose the
# path. A redirect to a path that is NOT there is a different matter: resolve_libvirt_uri reads
# it as "no lifecycle contract installed" and resolves qemu:///system with no message, exactly as
# it does on a bare dev host, because nothing here can tell the two apart. On a provisioned host
# that is the server/worker split this file exists to prevent, so the seam is only safe for a
# caller that stages a real file.
#
# Since #2509 there is a third consequence, because the preset branch reads LIBVIRT_ENV too: a
# redirect decides not only which value is published but whether the contradiction guard can fire
# at all. Pointed at an absent or invalid path it leaves the guard silent, so a preset that
# contradicts the real /etc entry is honoured with no report. What bounds THAT is not the
# allowlist — it is that the seam belongs to whoever owns the shell's environment, and they set
# KDIVE_LIBVIRT_URI in the first place. Do not restate the bound as "an explicit
# KDIVE_LIBVIRT_URI bypasses the allowlist anyway": the preset branch consults the contract but
# never adopts its value, and worker-lifecycle.sh calls load_published_libvirt_uri directly,
# where no bypass exists at all.
: "${LIBVIRT_ENV:=/etc/kdive/live-worker-libvirt.env}"
# Why the two names below carry no KDIVE_ prefix either (ADR-0659): check_env_documented.py sweeps
# scripts/ for KDIVE_[A-Z0-9_]+ and requires every hit to be a registry setting or a catalogued
# entry in kdive.config.external_env, which renders into the generated config reference. A
# prefixed name would have to be published there as an operator knob, which is exactly what an
# entry point's declaration about itself is not.
#
# LIBVIRT_OPTIONAL is read, never assigned, here: an entry point that can do useful work without
# libvirt exports it before sourcing lib.sh or env.sh. LIBVIRT_UNRESOLVED is this file's record of
# the resulting degraded state.
#
# Unlike LIBVIRT_ENV it is an OUTPUT, never an input, so it is not `:=`: resolve_libvirt_uri's
# first statement short-circuits on it, so a value arriving from the environment would disable
# endpoint resolution for every entry point, opted out or not -- KDIVE_LIBVIRT_URI would be left
# unset on a host whose contract is fine, and stack-status.sh would report that healthy host as
# unresolved with a caller-chosen reason string. It still has to survive the second source that
# stack-status.sh performs (lib.sh then env.sh, one shell), and the resolver this file is about
# to define is the one thing that tells a re-source from a fresh shell -- so the clear is
# conditional on that rather than on a second variable. Keep this line ABOVE that definition.
#
# The bound is an ordinary exported VARIABLE, which is the reachable case. It is not absolute: an
# ancestor that also `export -f resolve_libvirt_uri` makes the probe below see the function on the
# first source and keep an ambient record. That actor already owns the shell's function table --
# they could shadow `virsh` or `stat` instead -- so it buys them nothing, but do not read this
# line as proof against one.
declare -F resolve_libvirt_uri >/dev/null || LIBVIRT_UNRESOLVED=''
LIBVIRT_SOCKET_URIS=(
  'qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/libvirt-sock'
  'qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/virtqemud-sock'
)

require_exact_libvirt_env() {
  local metadata
  metadata="$(stat -c '%u:%g:%a' "$LIBVIRT_ENV" 2>/dev/null || true)"
  [[ -f "$LIBVIRT_ENV" && ! -L "$LIBVIRT_ENV" && "$metadata" == "0:0:644" ]] || {
    echo "lifecycle prerequisite has untrusted metadata: ${LIBVIRT_ENV}" >&2
    return 1
  }
}

load_published_libvirt_uri() {
  local lines uri allowed=0 candidate
  require_exact_libvirt_env || return 1
  mapfile -t lines <"$LIBVIRT_ENV"
  ((${#lines[@]} == 1)) && [[ "${lines[0]}" == KDIVE_LIBVIRT_URI=* ]] || {
    echo "${LIBVIRT_ENV} must contain exactly one KDIVE_LIBVIRT_URI assignment" >&2
    return 1
  }
  uri="${lines[0]#KDIVE_LIBVIRT_URI=}"
  for candidate in "${LIBVIRT_SOCKET_URIS[@]}"; do
    [[ "$uri" == "$candidate" ]] && allowed=1
  done
  ((allowed)) || {
    echo "${LIBVIRT_ENV} contains an unsupported session libvirt URI" >&2
    return 1
  }
  printf '%s' "$uri"
}

# Export the one host-local libvirt endpoint every consumer a live-stack entry point starts must
# share — server, reconciler, lifecycle worker, the `virsh` gates, teardown (#2480). An explicit
# caller value wins — reported, since #2509, where it contradicts a valid published contract;
# otherwise the published session URI when the lifecycle contract is installed;
# otherwise the bare-host default. lib.sh and env.sh both call it, so the endpoint no longer
# depends on which entry point brought the stack up.
#
# EXPORT, not assignment: restart_host_processes() forks the server and reconciler with the
# inherited environment, so an unexported value reached neither and both fell back to the
# in-process qemu:///system default while the worker used the published URI.
#
# Anything occupying the contract path but failing validation returns non-zero rather than falling
# back. Under the callers' `set -e` that aborts them, which is the point: a silent downgrade to
# qemu:///system is exactly the server/worker split this resolves, and it would happen under the
# one condition — an untrusted /etc file — where it matters most. The gate is `-e || -L` rather
# than `-e` alone because `-e` follows symlinks, so a dangling one would take the else branch and
# downgrade silently while a symlink to a valid contract is rejected.
#
# Every caller sources lib.sh or env.sh before doing any of its own work, so the abort takes the
# whole invocation with it — including ones that need no libvirt at all (`stack-services.sh
# --skip-libvirt`, apply-migrations.sh, onboard.sh) and the two that are most wanted when a host
# is broken, stack-down.sh and stack-status.sh. ADR-0659 settles that: the abort stays the
# default, and an entry point that can do useful work without libvirt exports LIBVIRT_OPTIONAL=1
# before sourcing, which downgrades the abort to the degraded state below. The override the
# message names is the way past it for everyone else, which is why it names it rather than only
# the cause.
#
# The degraded state leaves KDIVE_LIBVIRT_URI UNSET rather than empty. `virsh -c ''` connects to
# libvirt's probed default and exits 0, so an empty value would be the silent downgrade this
# whole file exists to prevent; unset makes every consumer that reads it without a `:-` default
# die with `unbound variable` under `set -u` instead. require_libvirt_uri below is how an
# opted-out entry point refuses the operations that do need the endpoint.
#
# One thing the override does not fix, recorded here because the abort's own reasoning invites the
# assumption that it does: a value naming the wrong daemon makes kdive_domains() enumerate one
# holding no kdive domains, so `stack-down.sh --wipe` reaps nothing and still removes the overlays.
# Since #2515 that is no longer silent — the zero-domain line names the endpoint it consulted,
# because an endpoint answering with nothing is either a clean host or the wrong one of the two
# URIs above and nothing there can tell them apart. Naming it is not detecting it, though: an empty
# listing leaves nothing unreaped, so the overlay removal still runs.
#
# The second defect this comment used to record is gone. #2515 made the reap report what it
# actually removed and exit 1 before `done` when anything survived, so a wipe that removes nothing
# no longer reports success. `destroy` still carries `|| true` there, deliberately: a domain that
# is already shut off answers non-zero, and that is not a reap failure.
resolve_libvirt_uri() {
  # Re-entry: stack-status.sh sources lib.sh and env.sh, and each calls this. Without the guard
  # the second call would re-enter (the endpoint is unset, so the -z test passes), repeat the
  # whole diagnosis, and resolve again against a contract that has not changed.
  #
  # It keys on the endpoint as well as the record, so what it short-circuits is "nothing has
  # changed since the degrade", not "this shell degraded once". A caller that supplies an
  # endpoint afterwards still reaches the export below -- which matters because ADR-0659 sends
  # #2509's next guard into the preset branch of this same function, and a write-once record
  # would have left require_libvirt_uri refusing an operation the shell can by then perform.
  if [[ -n "$LIBVIRT_UNRESOLVED" && -z "${KDIVE_LIBVIRT_URI:-}" ]]; then
    return 0
  fi
  if [[ -z "${KDIVE_LIBVIRT_URI:-}" ]]; then
    if [[ -e "$LIBVIRT_ENV" || -L "$LIBVIRT_ENV" ]]; then
      KDIVE_LIBVIRT_URI="$(load_published_libvirt_uri)" || {
        echo "to proceed anyway, export KDIVE_LIBVIRT_URI with one of the values" \
          "${LIBVIRT_ENV} is allowed to publish, or qemu:///system on a host with no" \
          "lifecycle contract; a value naming a daemon the kdive domains do not live on" \
          "leaves them defined while 'stack-down.sh --wipe' still removes their overlays" >&2
        [[ "${LIBVIRT_OPTIONAL:-0}" == "1" ]] || return 1
        # The failed command substitution left the endpoint set-but-empty; unset is the sentinel.
        unset KDIVE_LIBVIRT_URI
        LIBVIRT_UNRESOLVED="${LIBVIRT_ENV} failed validation"
        echo "continuing without a libvirt endpoint: ${LIBVIRT_UNRESOLVED}" >&2
        return 0
      }
    else
      KDIVE_LIBVIRT_URI=qemu:///system
    fi
  else
    # #2509, ADR-0661: an explicit override is deliberate and stays supported, but on a host that
    # publishes a contract one that disagrees puts this shell and the worker processes on
    # different daemons — the #2480 split, reached through the one path still allowed to be
    # silent. Report it and honour it; refusing would break the escape hatch the abort message
    # above, stack-status.sh and the live-testing runbook all send an operator to.
    #
    # Both halves of a loader failure are caught here, because this is the path that exists to
    # get past a broken contract. Its stderr is discarded: it writes its refusal BEFORE returning
    # 1, so an untrusted or allowlist-refused /etc entry would otherwise report itself on the one
    # path that must stay quiet. And the assignment is split from the declaration, because
    # `local v="$(f)"` takes `local`'s exit status, not f's — but a split assignment carries f's,
    # which under the callers' `set -e` aborts the sourcing shell, so the `||` is load-bearing
    # rather than decorative.
    local published
    published="$(load_published_libvirt_uri 2>/dev/null)" || published=''
    if [[ -n "$published" && "$KDIVE_LIBVIRT_URI" != "$published" ]]; then
      echo "KDIVE_LIBVIRT_URI is set to ${KDIVE_LIBVIRT_URI}, but ${LIBVIRT_ENV} publishes" \
        "${published}; honouring the override" >&2
      echo "every process this stack starts will use the override; unset KDIVE_LIBVIRT_URI to" \
        "use the published endpoint instead" >&2
    fi
  fi
  # Reached only with an endpoint in hand, so the record is stale by definition: clearing it here
  # is what stops require_libvirt_uri refusing after a repair. The degraded branch above returns
  # before this line, so it does not undo its own record.
  LIBVIRT_UNRESOLVED=''
  export KDIVE_LIBVIRT_URI
}

# Refuse <operation> while the endpoint is in the degraded state above, naming which operation was
# refused. Only a LIBVIRT_OPTIONAL entry point can reach that state, so this is where such an
# entry point's libvirt-dependent operations fail closed — at the point of use rather than at
# source time (ADR-0659). Returns 0 unchanged everywhere else, so a caller may gate on it
# unconditionally.
require_libvirt_uri() {
  [[ -z "$LIBVIRT_UNRESOLVED" ]] || {
    echo "cannot $1: ${LIBVIRT_UNRESOLVED}" >&2
    echo "repair ${LIBVIRT_ENV}, or export KDIVE_LIBVIRT_URI with one of the values it is" \
      "allowed to publish, and retry" >&2
    return 1
  }
}
