#!/usr/bin/env bash
# One-command local-libvirt developer bring-up. Idempotent; safe to re-run.
#
# Brings up the backends, applies the schema, seeds the project's budget/quota, starts the
# server/worker/reconciler as root on qemu:///system, blocks until the server reports ready,
# and merges the MCP client config into the kernel tree. Run it from anywhere — every path
# resolves from the repo.
#
#   examples/local-libvirt/up.sh
#
# Then, in the shell you launch your MCP client from:
#   export KDIVE_TOKEN=$(examples/local-libvirt/mint-token.sh)
set -euo pipefail

example_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${example_dir}/../.." && pwd)"
# shellcheck source=examples/local-libvirt/env.sh disable=SC1091
source "${example_dir}/env.sh"

pid_file="${KDIVE_STACK_PID_FILE}"
log_dir="${KDIVE_STACK_LOG_DIR}"
# The server's aux listener serves /readyz. Honour an explicit KDIVE_HEALTH_BIND_ADDR (the
# user owns the collision risk then — see README); otherwise the server's per-process
# default is 127.0.0.1:9464.
readyz_url="http://${KDIVE_HEALTH_BIND_ADDR:-127.0.0.1:9464}/readyz"

step() { printf '\n=== %s ===\n' "$1"; }

# Probe /readyz once; return 0 only on HTTP 200. Uses the venv interpreter's stdlib so the
# example needs neither curl nor jq.
probe_readyz() {
  "${KDIVE_PYTHON}" - "$1" <<'PY'
import sys
import urllib.request

try:
    with urllib.request.urlopen(sys.argv[1], timeout=2) as response:
        sys.exit(0 if response.status == 200 else 1)
except Exception:
    sys.exit(1)
PY
}

# Block until the server's /readyz is green, bailing early if any launched process has
# exited. `[[ -d /proc/$pid ]]` works regardless of process owner — `kill -0` on a root pid
# from a non-root shell returns EPERM and cannot tell "alive" from "gone".
wait_ready() {
  local url="$1"
  shift
  local pids=("$@") pid deadline
  deadline=$((SECONDS + 90))
  while ((SECONDS < deadline)); do
    for pid in "${pids[@]}"; do
      if [[ ! -d /proc/${pid} ]]; then
        echo "a stack process (pid ${pid}) exited before becoming ready" >&2
        return 1
      fi
    done
    if probe_readyz "${url}"; then
      return 0
    fi
    sleep 1
  done
  echo "timed out after 90s waiting for ${url} to return 200" >&2
  return 1
}

# 1. Preflight — fail early with actionable fixes (KVM, libvirt, qemu:///system, the worker
#    venv's drgn/libguestfs imports, and a writable install-staging directory).
step "preflight (check-local-libvirt.sh)"
"${repo_root}/scripts/check-local-libvirt.sh"

# 2. Refuse to stack a second root trio on top of a running one — duplicate processes would
#    fight over the same domains. Stop the existing one first.
step "guard against an already-running trio"
if pgrep -u 0 -f '[-]m kdive (server|worker|reconciler)' >/dev/null; then
  echo "a root kdive trio is already running; stop it first with ${example_dir}/down.sh" >&2
  exit 1
fi

# Prime sudo once, up front, so the steps below never block on a password prompt. It is
# re-validated immediately before the start step (the backend image pull can outlast the
# sudo timestamp).
step "sudo (root is required for qemu:///system, libguestfs, kexec, console-log reads)"
sudo -v

# 3. Install-staging directory — runs.install stages the built kernel/initrd here before
#    defining the domain. It must be worker-writable AND traversable by the qemu user, so it
#    lives under /var/lib (never $HOME, whose 0700 mode hides the staged kernel from qemu).
step "install staging ${KDIVE_INSTALL_STAGING}"
if [[ ! -w "${KDIVE_INSTALL_STAGING}" ]]; then
  sudo install -d -o "${USER}" -m 0755 "${KDIVE_INSTALL_STAGING}"
fi

# 4. Backends — Postgres, MinIO, mock-OIDC (host-published :5432 / :9000 / :8090), then the
#    one-shot bucket creator. docker compose runs as the invoking user.
step "backends (docker compose)"
(cd "${repo_root}" && docker compose up -d --wait postgres minio oidc)
(cd "${repo_root}" && docker compose run --rm minio-init)

# 5. Schema.
step "migrate"
(cd "${repo_root}" && "${KDIVE_PYTHON}" -m kdive migrate)

# 6. Onboard the project — budget/quota rows plus registration of the discovered local
#    libvirt resource. Token-less bootstrap (raw INSERTs), the correct path for a
#    single-developer box. (The product command is still named seed-demo; see issue #669.)
step "seed project '${KDIVE_PROJECT}'"
(cd "${repo_root}" && "${KDIVE_PYTHON}" -m kdive seed-demo \
  --project "${KDIVE_PROJECT}" \
  --limit-kcu "${KDIVE_LIMIT_KCU}" \
  --max-concurrent-allocations "${KDIVE_MAX_ALLOC}" \
  --max-concurrent-systems "${KDIVE_MAX_SYS}")

# 7. Start the three processes AS ROOT on qemu:///system. Root is the representative
#    identity: it manages system-scope domains, runs libguestfs/kexec, and reads the
#    root:0600 console log virtlogd writes.
#
#    All three start inside one `sudo -E bash -c`: each `nohup python -m kdive <proc>` execs
#    the interpreter, so `$!` is the real kdive pid (not a sudo/bash wrapper), and nohup
#    keeps it alive after the inner bash exits. The inner shell reads the env preserved by
#    `-E` (KDIVE_PYTHON / KDIVE_STACK_LOG_DIR). The three real pids land in the user-owned
#    pid file, which down.sh reads to stop them.
step "start server/worker/reconciler as root"
sudo -v
mkdir -p "${log_dir}" "$(dirname "${pid_file}")"
mapfile -t pids < <(sudo -E bash -c '
  set -euo pipefail
  for proc in server worker reconciler; do
    nohup "${KDIVE_PYTHON}" -m kdive "${proc}" >"${KDIVE_STACK_LOG_DIR}/${proc}.log" 2>&1 &
    echo "$!"
  done
')
printf '%s\n' "${pids[@]}" >"${pid_file}"

# 8. Block until the server is actually ready (pg + minio + oidc reachable). Only then is the
#    stack usable — a bare "it's up" banner after a fixed sleep would be a false green.
step "wait for /readyz (${readyz_url})"
if ! wait_ready "${readyz_url}" "${pids[@]}"; then
  echo "stack did not become ready; inspect the per-process logs:" >&2
  for proc in server worker reconciler; do
    echo "  ${log_dir}/${proc}.log" >&2
  done
  exit 1
fi

# 9. Merge the MCP client config into the kernel tree (the directory you open your MCP client
#    in). It references the token via ${KDIVE_TOKEN}, so the file holds no secret. An existing
#    .mcp.json is backed up to .mcp.json.bak and only its `kdive` server entry is replaced —
#    any other servers the user configured are preserved.
step "install MCP config into ${KDIVE_KERNEL_SRC}/.mcp.json"
"${KDIVE_PYTHON}" - "${example_dir}/mcp.json" "${KDIVE_KERNEL_SRC}/.mcp.json" <<'PY'
import json
import shutil
import sys
from pathlib import Path

template_path, target_path = Path(sys.argv[1]), Path(sys.argv[2])
entry = json.loads(template_path.read_text())["mcpServers"]["kdive"]

if target_path.exists():
    backup = target_path.parent / (target_path.name + ".bak")
    shutil.copy2(target_path, backup)
    doc = json.loads(target_path.read_text())
    if not isinstance(doc, dict):
        raise SystemExit(
            f"{target_path} is not a JSON object; refusing to overwrite (backup at {backup})"
        )
    doc.setdefault("mcpServers", {})["kdive"] = entry
    print(f"merged kdive entry into existing {target_path} (backup at {backup})")
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
  Logs    : ${log_dir}
  Stop    : ${example_dir}/down.sh

Next, in the shell you launch your MCP client from:
  export KDIVE_TOKEN=\$(${example_dir}/mint-token.sh)
EOF
