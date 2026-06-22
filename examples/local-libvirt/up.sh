#!/usr/bin/env bash
# One-command local-libvirt developer bring-up. Idempotent; safe to re-run.
#
# Brings up the backends, applies the schema, seeds the project's budget/quota, merges the
# MCP client config into the kernel tree, starts the server/worker/reconciler as root on
# qemu:///system, and blocks until all three report ready. Run it from anywhere — every path
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

# All three processes serve /readyz on the aux listener. With KDIVE_HEALTH_BIND_ADDR unset
# (the normal case) each process gets its own default port (server 9464, worker 9465,
# reconciler 9466), so the gate verifies the whole trio's dependency sets — not just the
# server's. If the user pins KDIVE_HEALTH_BIND_ADDR, all three would collide on that one port
# (see README); we then poll only it and rely on the per-pid exit check below to surface the
# worker/reconciler bind failure.
if [[ -n "${KDIVE_HEALTH_BIND_ADDR:-}" ]]; then
  readyz_urls=("http://${KDIVE_HEALTH_BIND_ADDR}/readyz")
else
  readyz_urls=(
    "http://127.0.0.1:9464/readyz"
    "http://127.0.0.1:9465/readyz"
    "http://127.0.0.1:9466/readyz"
  )
fi

step() { printf '\n=== %s ===\n' "$1"; }

# Probe every readyz URL once; return 0 only when ALL return HTTP 200. One interpreter
# invocation per poll (the venv's stdlib — no curl/jq dependency).
probe_all_ready() {
  "${KDIVE_PYTHON}" - "$@" <<'PY'
import sys
import urllib.request

for url in sys.argv[1:]:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            if response.status != 200:
                sys.exit(1)
    except Exception:
        sys.exit(1)
sys.exit(0)
PY
}

# Block until every readyz URL is green, bailing early if any launched process has exited.
# `[[ -d /proc/$pid ]]` works regardless of process owner — `kill -0` on a root pid from a
# non-root shell returns EPERM and cannot tell "alive" from "gone". Reads the module-level
# `readyz_urls`.
wait_ready() {
  local pids=("$@") pid deadline
  deadline=$((SECONDS + 90))
  while ((SECONDS < deadline)); do
    for pid in "${pids[@]}"; do
      if [[ ! -d /proc/${pid} ]]; then
        echo "a stack process (pid ${pid}) exited before becoming ready" >&2
        return 1
      fi
    done
    if probe_all_ready "${readyz_urls[@]}"; then
      return 0
    fi
    sleep 1
  done
  echo "timed out after 90s waiting for readiness (${readyz_urls[*]})" >&2
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
#    single-developer box.
step "seed project '${KDIVE_PROJECT}'"
(cd "${repo_root}" && "${KDIVE_PYTHON}" -m kdive seed-project \
  --project "${KDIVE_PROJECT}" \
  --limit-kcu "${KDIVE_LIMIT_KCU}" \
  --max-concurrent-allocations "${KDIVE_MAX_ALLOC}" \
  --max-concurrent-systems "${KDIVE_MAX_SYS}")

# 7. Merge the MCP client config into the kernel tree (the directory you open your MCP client
#    in) BEFORE starting the trio, so a failure here (missing/unwritable kernel tree, malformed
#    existing .mcp.json) stops the run cleanly instead of leaving the trio up and the run
#    blocked by the duplicate-run guard. It references the token via ${KDIVE_TOKEN}, so the
#    file holds no secret. An existing .mcp.json is preserved: its first version is backed up
#    to .mcp.json.bak (never overwritten on re-run) and only the `kdive` server entry is
#    replaced — any other servers the user configured are kept.
step "install MCP config into ${KDIVE_KERNEL_SRC}/.mcp.json"
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

# 8. Start the three processes AS ROOT on qemu:///system. Root is the representative
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
# A short count means the inner root shell never echoed three pids — almost always sudo
# unable to preserve KDIVE_* (sudoers env_reset / missing env_keep), or a process that could
# not exec. Fail loudly here rather than time out opaquely on readiness below.
if ((${#pids[@]} != 3)); then
  echo "expected 3 pids from the start step, got ${#pids[@]}; sudo may be unable to preserve" \
    "the environment (need 'sudo -E' / env_keep for KDIVE_*). Check ${log_dir}/*.log" >&2
  exit 1
fi

# 9. Block until all three processes are actually ready (each process's dependency set
#    reachable). Only then is the stack usable — a bare "it's up" banner after a fixed sleep
#    would be a false green.
step "wait for readiness (${readyz_urls[*]})"
if ! wait_ready "${pids[@]}"; then
  echo "stack did not become ready; inspect the per-process logs:" >&2
  for proc in server worker reconciler; do
    echo "  ${log_dir}/${proc}.log" >&2
  done
  exit 1
fi

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
