#!/usr/bin/env bash
# Source-only parser and resolver for the root-published live-worker libvirt endpoint.
#
# Sourced more than once per shell by design — lib.sh, env.sh and worker-lifecycle.sh each source
# it, and examples/local-libvirt/env.sh reaches it through env.sh — so these are plain assignments.
# `readonly` aborted the second source with "readonly variable" before the body ran, which under
# the callers' `set -euo pipefail` took the whole shell down.
#
# LIBVIRT_ENV is therefore overridable, which is what lets a test stage the contract outside /etc.
# That grants a caller nothing it does not already have: an explicit KDIVE_LIBVIRT_URI is honored
# verbatim below and bypasses this file's URI allowlist outright.
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
# A contract file that is present but fails validation returns non-zero rather than falling back.
# Under the callers' `set -e` that aborts bring-up, which is the point: a silent downgrade to
# qemu:///system is exactly the server/worker split this resolves, and it would happen under the
# one condition — an untrusted /etc file — where it matters most. Set KDIVE_LIBVIRT_URI explicitly
# to proceed anyway (teardown on a host whose contract is broken, say).
resolve_libvirt_uri() {
  if [[ -z "${KDIVE_LIBVIRT_URI:-}" ]]; then
    if [[ -e "$LIBVIRT_ENV" ]]; then
      KDIVE_LIBVIRT_URI="$(load_published_libvirt_uri)" || return 1
    else
      KDIVE_LIBVIRT_URI=qemu:///system
    fi
  fi
  export KDIVE_LIBVIRT_URI
}
