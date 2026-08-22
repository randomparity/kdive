#!/usr/bin/env bash
# Source-only parser for the root-published live-worker libvirt endpoint.

readonly LIBVIRT_ENV=/etc/kdive/live-worker-libvirt.env
readonly LIBVIRT_SOCKET_URIS=(
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
