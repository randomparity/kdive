#!/usr/bin/env bash
# One-command local-libvirt developer bring-up. Idempotent; safe to re-run.
#
# Brings up the backends, applies the schema, seeds the project's budget/quota, starts the
# server/worker/reconciler as root on qemu:///system, and installs the MCP client config
# into the kernel tree. Run it from anywhere — every path resolves from the repo.
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

step() { printf '\n=== %s ===\n' "$1"; }

# 1. Preflight — fail early with actionable fixes (KVM, libvirt, qemu:///system, the worker
#    venv's drgn/libguestfs imports, and a writable install-staging directory).
step "preflight (check-local-libvirt.sh)"
"${repo_root}/scripts/check-local-libvirt.sh"

# Prime sudo once, up front, so the backgrounded root processes below never block on a
# password prompt mid-bring-up.
step "sudo (root is required for qemu:///system, libguestfs, kexec, console-log reads)"
sudo -v

# 2. Install-staging directory — runs.install stages the built kernel/initrd here before
#    defining the domain. It must be worker-writable AND traversable by the qemu user, so it
#    lives under /var/lib (never $HOME, whose 0700 mode hides the staged kernel from qemu).
step "install staging ${KDIVE_INSTALL_STAGING}"
if [[ ! -w "${KDIVE_INSTALL_STAGING}" ]]; then
  sudo install -d -o "${USER}" -m 0755 "${KDIVE_INSTALL_STAGING}"
fi

# 3. Backends — Postgres, MinIO, mock-OIDC (host-published :5432 / :9000 / :8090), then the
#    one-shot bucket creator. docker compose runs as the invoking user.
step "backends (docker compose)"
(cd "${repo_root}" && docker compose up -d --wait postgres minio oidc)
(cd "${repo_root}" && docker compose run --rm minio-init)

# 4. Schema.
step "migrate"
(cd "${repo_root}" && "${KDIVE_PYTHON}" -m kdive migrate)

# 5. Onboard the project — budget/quota rows plus registration of the discovered local
#    libvirt resource. Token-less bootstrap (raw INSERTs), the correct path for a
#    single-developer box. (The product command is still named seed-demo; see issue #669.)
step "seed project '${KDIVE_PROJECT}'"
(cd "${repo_root}" && "${KDIVE_PYTHON}" -m kdive seed-demo \
  --project "${KDIVE_PROJECT}" \
  --limit-kcu "${KDIVE_LIMIT_KCU}" \
  --max-concurrent-allocations "${KDIVE_MAX_ALLOC}" \
  --max-concurrent-systems "${KDIVE_MAX_SYS}")

# 6. Start the three processes AS ROOT on qemu:///system. Root is the representative
#    identity: it manages system-scope domains, runs libguestfs/kexec, and reads the
#    root:0600 console log virtlogd writes. `sudo -E` preserves the KDIVE_*/AWS_* env; the
#    absolute venv interpreter avoids depending on root's PATH.
step "start server/worker/reconciler as root"
mkdir -p "${log_dir}" "$(dirname "${pid_file}")"
: >"${pid_file}"
start_one() {
  local name="$1"
  # The redirect runs as the invoking user (not root), so the log files stay readable
  # without sudo — that is intentional here, not the SC2024 hazard.
  # shellcheck disable=SC2024
  sudo -E "${KDIVE_PYTHON}" -m kdive "${name}" >"${log_dir}/${name}.log" 2>&1 &
  echo "$!" >>"${pid_file}"
}
start_one server
start_one worker
start_one reconciler
sleep 1

# 7. Install the MCP client config into the kernel tree (the directory you open your MCP
#    client in). It references the token via ${KDIVE_TOKEN}, so the file holds no secret.
step "install MCP config into ${KDIVE_KERNEL_SRC}/.mcp.json"
install -m 0644 "${example_dir}/mcp.json" "${KDIVE_KERNEL_SRC}/.mcp.json"

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
