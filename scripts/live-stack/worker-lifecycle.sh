#!/usr/bin/env bash
# Request bounded host-worker lifecycle operations through the provisioned witness.
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/live-stack/lib.sh
source "${here}/lib.sh"
# shellcheck disable=SC1091 # repo-relative env script
source "${here}/env.sh"
# shellcheck source=scripts/live-stack/libvirt-uri.sh
source "${here}/libvirt-uri.sh"

readonly LIFECYCLE_SOCKET=/run/kdive/live-worker-lifecycle.sock
readonly LIFECYCLE_REVISION=/opt/kdive-live-worker-lifecycle/revision
readonly WORKER_PYTHON=/opt/kdive-live-worker-lifecycle/.venv/bin/python

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

require_start_prerequisites() {
  local slot component_root expected_revision actual_revision control_group
  local component_roots=()
  [[ "$WORKER_PYTHON" == /* && -f "$WORKER_PYTHON" ]] || {
    echo "installed worker Python is missing: ${WORKER_PYTHON}" >&2
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
  for component_root in "$KDIVE_ROOTFS_DIR" "$KDIVE_BUILD_WORKSPACE" "$KDIVE_INSTALL_STAGING" \
    "$KDIVE_FIXTURE_CATALOG_PATH"; do
    [[ "$component_root" == /* && -d "$component_root" ]] || {
      echo "worker provider path must be an existing absolute directory: ${component_root}" >&2
      return 1
    }
  done
  IFS=: read -r -a component_roots <<<"$KDIVE_BUILD_COMPONENT_ROOTS"
  for component_root in "${component_roots[@]}"; do
    [[ "$component_root" == /* && -d "$component_root" ]] || {
      echo "worker provider path must be an existing absolute directory: ${component_root}" >&2
      return 1
    }
  done
  require_worker_path_access "$WORKER_PYTHON" rx "installed worker Python" || return 1
  require_worker_path_access "${KDIVE_KERNEL_SRC}" rx "worker kernel source" || return 1
  require_worker_path_access "$KDIVE_ROOTFS_DIR" rwx "worker rootfs directory" || return 1
  require_worker_path_access "$KDIVE_BUILD_WORKSPACE" rwx "worker build workspace" || return 1
  require_worker_path_access "$KDIVE_INSTALL_STAGING" rwx "worker install staging" || return 1
  require_worker_path_access "$KDIVE_FIXTURE_CATALOG_PATH" rx "worker fixture catalog" || return 1
  for component_root in "${component_roots[@]}"; do
    require_worker_path_access "$component_root" rx "worker build component root" || return 1
  done
}

account_permission_bits() {
  local account_uid="$1" account_groups="$2" owner="$3" group="$4" mode="$5"
  mode="000${mode}"
  mode="${mode: -3}"
  if [[ $owner == "$account_uid" ]]; then
    printf '%s' "${mode:0:1}"
  elif [[ $account_groups == *" $group "* ]]; then
    printf '%s' "${mode:1:1}"
  else
    printf '%s' "${mode:2:1}"
  fi
}

permission_mask() {
  case "$1" in
  r) printf 4 ;;
  w) printf 2 ;;
  x) printf 1 ;;
  *) return 1 ;;
  esac
}

has_permissions() {
  local bits="$1" required="$2" permission mask position
  for ((position = 0; position < ${#required}; position++)); do
    permission="${required:position:1}"
    mask="$(permission_mask "$permission")" || return 1
    ((8#$bits & mask)) || return 1
  done
}

component_is_accessible() {
  local bits="$1" is_target="$2" required="$3"
  if [[ $is_target == true ]]; then
    has_permissions "$bits" "$required"
  else
    has_permissions "$bits" x
  fi
}

account_has_path_access() {
  local account="$1" target="$2" final_permissions="$3"
  local account_uid account_groups current resolved metadata owner group mode bits
  local index target_component
  local -a path_parts=()
  account_uid="$(id -u "$account")"
  account_groups=" $(id -G "$account") "
  resolved="$(realpath -e -- "$target")" || return 1
  current="$resolved"
  while :; do
    path_parts+=("$current")
    [[ $current == / ]] && break
    current="${current%/*}"
    [[ -n $current ]] || current=/
  done
  for ((index = ${#path_parts[@]} - 1; index >= 0; index--)); do
    current="${path_parts[index]}"
    metadata="$(stat -Lc '%u:%g:%a' "$current" 2>/dev/null)" || return 1
    IFS=: read -r owner group mode <<<"$metadata"
    bits="$(account_permission_bits "$account_uid" "$account_groups" "$owner" "$group" "$mode")"
    [[ $index -ne 0 ]] && target_component=false || target_component=true
    component_is_accessible "$bits" "$target_component" "$final_permissions" || return 1
  done
}

require_worker_path_access() {
  local target="$1" permissions="$2" description="$3" slot
  for slot in {1..8}; do
    account_has_path_access "kdive-worker-${slot}" "$target" "$permissions" || {
      echo "${description} is not accessible to kdive-worker-${slot};" \
        "fix ownership or group mode" >&2
      return 1
    }
  done
}

request() {
  local operation="$1" count="${2:-}" libvirt_uri=""
  if [[ "$operation" == start ]]; then
    require_start_prerequisites || return 1
    libvirt_uri="$(load_published_libvirt_uri)" || return 1
  fi
  env -u KDIVE_DATABASE_URL -u KDIVE_MIGRATION_DATABASE_URL -u KDIVE_SERVER_DATABASE_URL \
    -u KDIVE_RECONCILER_DATABASE_URL KDIVE_LIFECYCLE_OPERATION="$operation" \
    KDIVE_LIFECYCLE_COUNT="$count" \
    KDIVE_LIFECYCLE_LIBVIRT_URI="$libvirt_uri" KDIVE_WORKER_PYTHON="$WORKER_PYTHON" \
    KDIVE_SOURCE_ROOT="${KDIVE_KERNEL_SRC:-}" \
    KDIVE_EXPECTED_SLOTS="${KDIVE_LIFECYCLE_EXPECTED_SLOTS:-}" "$py" - <<'PY'
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from kdive.processes.lifecycle.systemd_worker_control import ProtocolRejected, request_path
from kdive.processes.lifecycle.systemd_worker_contract import LifecycleRequest, client_exit_status

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
                    "python": os.environ["KDIVE_WORKER_PYTHON"],
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
except (KeyError, TypeError, ValueError, ValidationError):
    print("lifecycle request construction failed safely", file=sys.stderr)
    raise SystemExit(2) from None

try:
    response = request_path(Path("/run/kdive/live-worker-lifecycle.sock"), request)
except (OSError, ProtocolRejected):
    print("lifecycle request transport failed safely", file=sys.stderr)
    raise SystemExit(5) from None

if not response.ok:
    print(response.model_dump_json())
    raise SystemExit(client_exit_status(response))

try:
    expected = os.environ["KDIVE_EXPECTED_SLOTS"]
    if expected:
        expected_count = int(expected)
        if not 1 <= expected_count <= 8:
            raise ValueError
        want = tuple(range(1, expected_count + 1))
        actual = tuple(
            (slot.slot, slot.phase.value if slot.phase else None)
            for slot in response.slots
        )
        if actual != tuple((slot, "started") for slot in want):
            raise ValueError("lifecycle status does not report the requested started slots")
except ValueError:
    print("lifecycle status does not report the requested started slots", file=sys.stderr)
    raise SystemExit(5) from None
print(response.model_dump_json())
raise SystemExit(client_exit_status(response))
PY
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
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
    [[ $# == 1 ]] || {
      usage
      exit 2
    }
    request "$1"
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
fi
