#!/usr/bin/env bash
# One-command local-libvirt developer bring-up. Idempotent; safe to re-run.
#
# Brings up the whole stack through the maintained host flow (scripts/live-stack/up.sh: compose
# backends, migrations, runtime-role bootstrap, the operator-owned session libvirt, the
# server/reconciler daemons, the fixed lifecycle workers, one inventory reconcile), funds the
# project and mints a token (scripts/live-stack/onboard.sh), and merges the MCP client config into
# the kernel tree. Run it from anywhere — every path resolves from the repo.
#
#   examples/local-libvirt/up.sh
#
# Then, in the shell you launch your MCP client from:
#   export KDIVE_TOKEN=$(examples/local-libvirt/mint-token.sh)
#
# Runs as the operator (never root): the workers run in their own fixed accounts under the
# root-owned lifecycle witness install-host.sh installed, and the daemons run as you. Every
# libvirt consumer shares the session daemon that contract published (env.sh).
set -euo pipefail

example_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${example_dir}/../.." && pwd)"
# shellcheck source=examples/local-libvirt/env.sh disable=SC1091
source "${example_dir}/env.sh"

step() { printf '\n=== %s ===\n' "$1"; }

if ((EUID == 0)); then
  echo "run up.sh as the operator user install-host.sh prepared, not as root" >&2
  exit 1
fi

# 1. The lifecycle contract's control group. install-host.sh adds the operator to it, and group
#    membership is fixed at login, so a shell that predates the install lacks it and every
#    witness request would be refused — say so here instead of failing mid-bring-up.
step "lifecycle contract"
if ! id -nG | tr ' ' '\n' | grep -qx kdive-live-control; then
  echo "${USER} is not in the kdive-live-control group in this shell. Either the fixed worker" >&2
  echo "lifecycle is not installed (run ${example_dir}/install-host.sh) or this login predates" >&2
  echo "it: log out and back in, then re-run." >&2
  exit 1
fi
[[ "${KDIVE_LIBVIRT_URI}" == *"live-libvirt"* ]] || {
  echo "no published session libvirt endpoint (${LIBVIRT_ENV}); run ${example_dir}/install-host.sh" >&2
  exit 1
}
echo "operator ${USER}; libvirt ${KDIVE_LIBVIRT_URI}"

# 2. Preflight — fail early with actionable fixes (KVM, libvirt, a writable install-staging
#    directory). The worker venv's drgn/libguestfs imports gate only the kdump capture method,
#    so a first-run box gets a WARN with the fix rather than a stop; export
#    KDIVE_PREFLIGHT_KDUMP=required to insist on it.
step "preflight (check-local-libvirt.sh)"
KDIVE_PREFLIGHT_KDUMP="${KDIVE_PREFLIGHT_KDUMP:-optional}" \
  "${repo_root}/scripts/operations/check-local-libvirt.sh"

# 3. The stack. Prometheus/grafana are not part of a first run; KDIVE_SKIP_OBS=0 brings them up.
step "stack (scripts/live-stack/up.sh)"
stack_args=()
[[ "${KDIVE_SKIP_OBS:-1}" == "1" ]] && stack_args+=(--skip-obs)
"${repo_root}/scripts/live-stack/up.sh" "${stack_args[@]}"

# 4. Fund the project (budget + quota rows, verified) and mint a token. Token-less bootstrap
#    (raw INSERTs), the correct path for a single-developer box; the printed
#    `export KDIVE_TOKEN=...` line is the same token mint-token.sh re-mints on demand.
step "onboard project '${KDIVE_PROJECT}' (scripts/live-stack/onboard.sh)"
"${repo_root}/scripts/live-stack/onboard.sh"

# 5. Merge the MCP client config into the kernel tree (the directory you open your MCP client
#    in). It references the token via ${KDIVE_TOKEN}, so the file holds no secret. An existing
#    .mcp.json is preserved: its first version is backed up to .mcp.json.bak (never overwritten
#    on re-run) and only the `kdive` server entry is replaced — any other servers the user
#    configured are kept.
step "install MCP config into ${KDIVE_KERNEL_SRC:-<unset>}/.mcp.json"
if [[ -z "${KDIVE_KERNEL_SRC:-}" ]]; then
  echo "KDIVE_KERNEL_SRC is unset and ~/src/linux does not exist; clone a kernel tree there or" >&2
  echo "export KDIVE_KERNEL_SRC, then re-run to install the .mcp.json (the stack is up)." >&2
  exit 1
fi
"${KDIVE_PYTHON}" - "${example_dir}/mcp.json" "${KDIVE_KERNEL_SRC}/.mcp.json" <<'PY'
import json
import shutil
import sys
from pathlib import Path

template_path, target_path = Path(sys.argv[1]), Path(sys.argv[2])
entry = json.loads(template_path.read_text())["mcpServers"]["kdive"]

if not target_path.parent.is_dir():
    raise SystemExit(
        f"kernel tree {target_path.parent} does not exist; set KDIVE_KERNEL_SRC to your "
        "checkout (see README)"
    )

if target_path.exists():
    backup = target_path.parent / (target_path.name + ".bak")
    try:
        doc = json.loads(target_path.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"{target_path} is not valid JSON ({exc}); fix or remove it, then re-run"
        ) from exc
    if not isinstance(doc, dict):
        raise SystemExit(f"{target_path} is not a JSON object; fix or remove it, then re-run")
    # First original wins: never clobber an existing backup with an already-merged file.
    if not backup.exists():
        shutil.copy2(target_path, backup)
        print(f"backed up existing {target_path} to {backup}")
    doc.setdefault("mcpServers", {})["kdive"] = entry
    print(f"merged kdive entry into {target_path}")
else:
    doc = {"mcpServers": {"kdive": entry}}
    print(f"created {target_path}")

target_path.write_text(json.dumps(doc, indent=2) + "\n")
PY

cat <<EOF

local-libvirt stack is up.
  MCP URL : ${KDIVE_STACK_BASE_URL}
  Project : ${KDIVE_PROJECT} (admin)
  Kernel  : ${KDIVE_KERNEL_SRC}
  libvirt : ${KDIVE_LIBVIRT_URI}
  Logs    : ${KDIVE_STACK_LOG_DIR} (daemons); scripts/live-stack/worker-lifecycle.sh diagnostics (workers)
  Status  : ${repo_root}/scripts/live-stack/status.sh
  Stop    : ${example_dir}/down.sh

Next, in the shell you launch your MCP client from:
  export KDIVE_TOKEN=\$(${example_dir}/mint-token.sh)

No guest image yet? Build and register one (Fedora 44 is the kdump-capable default):
  ${example_dir}/build-image.sh fedora-kdive-ready-44
EOF
