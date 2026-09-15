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
# A redirect to a file that IS there reaches the same validation /etc does — a non-symlink regular
# file `stat` reports as root:root 0644, holding exactly one KDIVE_LIBVIRT_URI line naming one of
# the two URIs in LIBVIRT_SOCKET_URIS — so it can only ever yield a value that was already
# correct. A redirect to a path that is NOT there is a different matter: resolve_libvirt_uri reads
# it as "no lifecycle contract installed" and resolves qemu:///system with no message, exactly as
# it does on a bare dev host, because nothing here can tell the two apart. On a provisioned host
# that is the server/worker split this file exists to prevent, so the seam is only safe for a
# caller that stages a real file. Do not restate the bound as "an explicit KDIVE_LIBVIRT_URI
# bypasses the allowlist anyway": that holds for resolve_libvirt_uri only, and
# worker-lifecycle.sh calls load_published_libvirt_uri directly, where no such bypass exists.
: "${LIBVIRT_ENV:=/etc/kdive/live-worker-libvirt.env}"
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
# caller value wins; otherwise the published session URI when the lifecycle contract is installed;
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
# is broken, stack-down.sh and stack-status.sh. Whether a libvirt-free entry point should survive
# a broken contract is a scope question this change does not settle; the override below is the
# way past it either way, which is why the message names it rather than only the cause.
#
# Two things the override does not fix, recorded here because the abort's own reasoning invites
# the assumption that it does. A value naming the wrong daemon makes kdive_domains() query one
# holding no kdive domains, so `stack-down.sh --wipe` reaps nothing and still removes the
# overlays. And even under the right value the reap is not observable: stack-down.sh suffixes
# its destroy, undefine and rm with `|| true`, so on the sudo-less provisioned service account all
# three fail silently and teardown prints `done` having reaped nothing. Both belong to
# stack-down.sh.
resolve_libvirt_uri() {
  if [[ -z "${KDIVE_LIBVIRT_URI:-}" ]]; then
    if [[ -e "$LIBVIRT_ENV" || -L "$LIBVIRT_ENV" ]]; then
      KDIVE_LIBVIRT_URI="$(load_published_libvirt_uri)" || {
        echo "to proceed anyway, export KDIVE_LIBVIRT_URI with one of the values" \
          "${LIBVIRT_ENV} is allowed to publish, or qemu:///system on a host with no" \
          "lifecycle contract; a value naming a daemon the kdive domains do not live on" \
          "leaves them defined while 'stack-down.sh --wipe' still removes their overlays" >&2
        return 1
      }
    else
      KDIVE_LIBVIRT_URI=qemu:///system
    fi
  fi
  export KDIVE_LIBVIRT_URI
}
