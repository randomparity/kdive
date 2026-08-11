#!/usr/bin/env bash
# Request bounded host-worker lifecycle operations through the provisioned witness.
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/live-stack/lib.sh
source "${here}/lib.sh"
# shellcheck disable=SC1091 # repo-relative env script
source "${here}/env.sh"

readonly LIBVIRT_ENV=/etc/kdive/live-worker-libvirt.env
readonly LIFECYCLE_SOCKET=/run/kdive/live-worker-lifecycle.sock
readonly LIFECYCLE_REVISION=/opt/kdive-live-worker-lifecycle/revision
readonly LIBVIRT_SOCKET_URIS=(
  'qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/libvirt-sock'
  'qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/virtqemud-sock'
)

usage() {
  echo "usage: scripts/live-stack/worker-lifecycle.sh start COUNT|status|stop|diagnostics" >&2
}

require_exact_file() {
  local file="$1" mode="$2" metadata
  metadata="$(stat -c '%u:%g:%a' "$file" 2>/dev/null || true)"
  [[ -f "$file" && ! -L "$file" && "$metadata" == "0:0:${mode}" ]] || {
    echo "lifecycle prerequisite has untrusted metadata: ${file}" >&2
    return 1
  }
}

load_libvirt_uri() {
  local lines uri allowed=0
  require_exact_file "$LIBVIRT_ENV" 644 || return 1
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

require_start_prerequisites() {
  local slot component_root expected_revision actual_revision control_group
  [[ "$py" == /* && -x "$py" ]] || {
    echo "worker Python must be an executable absolute path: ${py}" >&2
    return 1
  }
  [[ "${KDIVE_KERNEL_SRC:-}" == /* && -d "${KDIVE_KERNEL_SRC}" ]] || {
    echo "worker kernel source must be an existing absolute directory" >&2
    return 1
  }
  for slot in {1..8}; do
    id "kdive-worker-${slot}" >/dev/null 2>&1 || {
      echo "missing provisioned worker account kdive-worker-${slot}" >&2
      return 1
    }
  done
  control_group="$(getent group kdive-live-control | cut -d: -f3)"
  [[ -n "$control_group" ]] || {
    echo "missing provisioned kdive-live-control group" >&2
    return 1
  }
  [[ -S "$LIFECYCLE_SOCKET" ]] &&
    [[ "$(stat -c '%u:%g:%a' "$LIFECYCLE_SOCKET")" == "0:${control_group}:660" ]] || {
    echo "lifecycle socket is missing or has untrusted metadata: ${LIFECYCLE_SOCKET}" >&2
    return 1
  }
  require_exact_file "$LIFECYCLE_REVISION" 444 || return 1
  expected_revision="$(git -C "$repo_root" rev-parse HEAD)"
  actual_revision="$(<"$LIFECYCLE_REVISION")"
  [[ "$actual_revision" == "$expected_revision" ]] || {
    echo "installed lifecycle witness revision does not match this checkout" >&2
    return 1
  }
  mkdir -p "$KDIVE_BUILD_WORKSPACE"
  for component_root in "$KDIVE_ROOTFS_DIR" "$KDIVE_BUILD_WORKSPACE" "$KDIVE_INSTALL_STAGING" \
    "$KDIVE_FIXTURE_CATALOG_PATH"; do
    [[ "$component_root" == /* && -d "$component_root" ]] || {
      echo "worker provider path must be an existing absolute directory: ${component_root}" >&2
      return 1
    }
  done
}

request() {
  local operation="$1" count="${2:-}" libvirt_uri=""
  if [[ "$operation" == start ]]; then
    require_start_prerequisites || return 1
    libvirt_uri="$(load_libvirt_uri)" || return 1
  fi
  env -u KDIVE_DATABASE_URL -u KDIVE_MIGRATION_DATABASE_URL -u KDIVE_SERVER_DATABASE_URL \
    -u KDIVE_RECONCILER_DATABASE_URL KDIVE_LIFECYCLE_OPERATION="$operation" \
    KDIVE_LIFECYCLE_COUNT="$count" \
    KDIVE_LIFECYCLE_LIBVIRT_URI="$libvirt_uri" KDIVE_PYTHON="$py" \
    KDIVE_SOURCE_ROOT="${KDIVE_KERNEL_SRC:-}" KDIVE_EXPECTED_SLOTS="${3:-}" "$py" - <<'PY'
import os
import sys
from pathlib import Path

from kdive.processes.lifecycle.systemd_worker_control import ProtocolRejected, request_path
from kdive.processes.lifecycle.systemd_worker_contract import LifecycleRequest

try:
    operation = os.environ["KDIVE_LIFECYCLE_OPERATION"]
    if operation != "start":
        request = LifecycleRequest.model_validate({"operation": operation})
    else:
        count = int(os.environ["KDIVE_LIFECYCLE_COUNT"])
        lanes = tuple(filter(None, os.environ["KDIVE_ACCEPTED_LANES"].split(",")))
        binds = {
            slot: f"127.0.0.1:{9465 if slot == 1 else 9468 + slot}"
            for slot in range(1, count + 1)
        }
        request = LifecycleRequest.model_validate(
            {
                "operation": "start",
                "worker_count": count,
                "settings": {
                "python": os.environ["KDIVE_PYTHON"],
                "source_root": os.environ["KDIVE_SOURCE_ROOT"],
                "rootfs_dir": os.environ["KDIVE_ROOTFS_DIR"],
                "build_workspace": os.environ["KDIVE_BUILD_WORKSPACE"],
                "build_component_roots": os.environ["KDIVE_BUILD_COMPONENT_ROOTS"],
                "install_staging": os.environ["KDIVE_INSTALL_STAGING"],
                "fixture_catalog_path": os.environ["KDIVE_FIXTURE_CATALOG_PATH"],
                "worker_database_url": os.environ["KDIVE_WORKER_DATABASE_URL"],
                "libvirt_uri": os.environ["KDIVE_LIFECYCLE_LIBVIRT_URI"],
                "s3_endpoint_url": os.environ["KDIVE_S3_ENDPOINT_URL"],
                "s3_bucket": os.environ["KDIVE_S3_BUCKET"],
                "s3_region": os.environ["KDIVE_S3_REGION"],
                "aws_access_key_id": os.environ["AWS_ACCESS_KEY_ID"],
                "aws_secret_access_key": os.environ["AWS_SECRET_ACCESS_KEY"],
                "accepted_lanes": lanes,
                "build_user": os.environ.get("KDIVE_BUILD_USER", os.environ["USER"]),
                "log_level": os.environ["KDIVE_LOG_LEVEL"],
                "health_binds": binds,
                },
            }
        )
    response = request_path(Path("/run/kdive/live-worker-lifecycle.sock"), request)
    expected = os.environ["KDIVE_EXPECTED_SLOTS"]
    if expected:
        want = tuple(range(1, int(expected) + 1))
        actual = tuple((slot.slot, slot.phase.value if slot.phase else None) for slot in response.slots)
        if actual != tuple((slot, "started") for slot in want):
            raise ValueError("lifecycle status does not report the requested started slots")
except Exception:
    print("lifecycle request failed safely", file=sys.stderr)
    raise SystemExit(5) from None
print(response.model_dump_json())
raise SystemExit(0 if response.ok and response.code == "ok" else 4)
PY
}

case "${1:-}" in
start)
  [[ "${2:-}" =~ ^[1-8]$ ]] || {
    usage
    exit 2
  }
  [[ $# == 2 ]] || {
    usage
    exit 2
  }
  request start "$2"
  ;;
status)
  [[ $# == 1 || $# == 2 ]] || {
    usage
    exit 2
  }
  request "$1" "" "${2:-}"
  ;;
stop | diagnostics)
  [[ $# == 1 ]] || {
    usage
    exit 2
  }
  request "$1"
  ;;
*)
  usage
  exit 2
  ;;
esac
