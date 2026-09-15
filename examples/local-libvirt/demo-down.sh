#!/usr/bin/env bash
# Stop the local-libvirt example stack started by demo-up.sh: the fixed lifecycle workers (through
# the witness), the server/reconciler daemons, and the compose backends. State is kept —
# the compose data volumes and any running kdive-* domains survive a plain stop.
#
#   examples/local-libvirt/demo-down.sh            stop, keep state
#   examples/local-libvirt/demo-down.sh --wipe     also drop the database, the artifacts bucket, and
#                                             reap kdive-provisioned domains + overlays
#   examples/local-libvirt/demo-down.sh --wipe --yes   skip the confirmation prompt
#
# The whole teardown is scripts/live-stack/stack-down.sh; this wrapper only adds the example env
# (log directory under $XDG_STATE_HOME, the published session libvirt endpoint).
set -euo pipefail

example_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${example_dir}/../.." && pwd)"
# shellcheck source=examples/local-libvirt/env.sh disable=SC1091
source "${example_dir}/env.sh"

exec "${repo_root}/scripts/live-stack/stack-down.sh" "$@"
