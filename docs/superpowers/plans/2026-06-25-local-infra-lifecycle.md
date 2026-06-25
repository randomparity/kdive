# Local Infrastructure Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide repeatable `up`/`down`/`status` scripts that bring up the whole local kdive stack (compose backends + DB migrations + libvirt + host processes), and add Grafana to the compose `obs` profile.

**Architecture:** Four bash scripts under `scripts/live-stack/` sharing a sourced `lib.sh`. `up.sh` orchestrates layers in order and delegates the host-process tier to the existing `restart-stack.sh`; `down.sh` tears down with an opt-in `--wipe`; `status.sh` is read-only. Grafana becomes a profiled compose service with file-provisioned datasource + dashboard.

**Tech Stack:** Bash (`set -euo pipefail`, shfmt 2-space), Docker Compose, libvirt (`virsh`, `qemu:///system`), Python/pytest (invariant guard), Grafana 13.0.3 + Prometheus.

## Global Constraints

- Bash: every script starts with `set -euo pipefail`; lint clean under `shellcheck` and `shfmt -i 2 -d`.
- Absolute/repo-relative paths only; scripts resolve `repo_root` from `${BASH_SOURCE[0]}` (two levels up from `scripts/live-stack/`).
- Pin container images to explicit tags — never `:latest`. Grafana = `grafana/grafana:13.0.3`.
- The compose `kdive:dev` app tier (`migrate`, `server`, `worker`, `reconciler`) must NEVER be started by these scripts. The host processes own the app tier; the host `apply-migrations.sh` is the authoritative migrator.
- libvirt provider uses QEMU user-mode SLIRP networking and `qemu-img` qcow2 overlays in `/var/lib/kdive/rootfs` — do NOT create or validate any libvirt network or storage pool.
- The worker runs as root and needs `KDIVE_KERNEL_SRC` + `KDIVE_BUILD_USER` (inherited from `restart-stack.sh`; ADR-0214).
- Secrets/credentials in compose are local-dev only and already carry `# pragma: allowlist secret` where the detect-secrets hook requires it.

---

## File Structure

- Create `scripts/live-stack/lib.sh` — shared constants + functions (sourced, no side effects).
- Modify `scripts/live-stack/restart-stack.sh` — source `lib.sh`, drop the now-shared copies (behavior-preserving).
- Create `scripts/live-stack/status.sh` — read-only per-layer health report.
- Modify `docker-compose.yml` — add `grafana` service under the `obs` profile.
- Create `deploy/compose/grafana/provisioning/datasources/prometheus.yml` — default Prometheus datasource.
- Create `deploy/compose/grafana/provisioning/dashboards/kdive.yml` — file dashboard provider.
- Create `scripts/live-stack/up.sh` — ordered full bring-up + `--reset-db`.
- Create `scripts/live-stack/down.sh` — teardown + `--wipe` (reaps libvirt domains/overlays).
- Create `tests/live_stack/test_up_invariants.py` — automated guard for the two invariants.
- Modify `deploy/compose/README.md` — document the lifecycle scripts + Grafana.

---

### Task 1: Shared `lib.sh` + refactor `restart-stack.sh`

**Files:**
- Create: `scripts/live-stack/lib.sh`
- Modify: `scripts/live-stack/restart-stack.sh`

**Interfaces:**
- Produces (sourced by later tasks): vars `repo_root`, `py`, `log_dir`, array `KDIVE_BACKEND_SERVICES`, `KDIVE_LIBVIRT_URI`, `KDIVE_ROOTFS_DIR`; functions `daemon_pids`, `stop_daemons`, `report_build_stamps`, `server_health` (returns 0 iff HTTP 401), `libvirt_ok`, `provision_prereqs_ok`, `kdive_domains`.

- [ ] **Step 1: Write `scripts/live-stack/lib.sh`**

```bash
#!/usr/bin/env bash
# Shared helpers for the local live-stack lifecycle scripts (up.sh, down.sh, status.sh,
# restart-stack.sh). SOURCED, never executed: it defines variables and functions and must
# have no side effects beyond that. Consumers source env.sh themselves when they need the
# KDIVE_* runtime config.

# scripts/live-stack/ -> repo root is two levels up (matches start.sh / restart-stack.sh).
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
py="${repo_root}/.venv/bin/python"
log_dir="${KDIVE_STACK_LOG_DIR:-${repo_root}/.live-stack-logs}"

# Canonical backend compose services. NEVER the kdive:dev app tier (migrate/server/worker/
# reconciler) — the host processes own that tier.
KDIVE_BACKEND_SERVICES=(postgres minio minio-init oidc)

# The local-libvirt provider connects here (KDIVE_LIBVIRT_URI, default qemu:///system) and
# stores per-System qcow2 overlays under KDIVE_ROOTFS_DIR. It uses user-mode SLIRP networking
# and qemu-img overlays — NO libvirt network or storage pool is involved.
KDIVE_LIBVIRT_URI="${KDIVE_LIBVIRT_URI:-qemu:///system}"
KDIVE_ROOTFS_DIR="${KDIVE_ROOTFS_DIR:-/var/lib/kdive/rootfs}"

# Matches the real daemon argv, e.g. ".venv/bin/python -m kdive server". `[.]` is a literal
# dot, so awk's dynamic-regex engine does not warn about an unescaped metacharacter.
_daemon_match='[.]venv/bin/python -m kdive (server|worker|reconciler)'

# PIDs of the real python daemons only — comm must be python, so a `bash -c '... kdive worker'`
# launcher wrapper (whose argv also matches) is excluded.
daemon_pids() {
  ps -eo pid=,comm=,args= | awk -v re="$_daemon_match" '$2 ~ /^python/ && $0 ~ re {print $1}'
}

stop_daemons() {
  local pids pid owner
  mapfile -t pids < <(daemon_pids)
  ((${#pids[@]})) || {
    echo "no kdive daemons running"
    return 0
  }
  echo "stopping kdive daemons: ${pids[*]}"
  for pid in "${pids[@]}"; do
    owner="$(ps -o user= -p "$pid" 2>/dev/null || true)"
    if [[ "$owner" == "root" && "$(id -un)" != "root" ]]; then
      sudo kill "$pid" 2>/dev/null || true
    else
      kill "$pid" 2>/dev/null || true
    fi
  done
  for _ in {1..20}; do
    [[ -z "$(daemon_pids)" ]] && return 0
    sleep 0.5
  done
  echo "WARN: daemons still running after stop: $(daemon_pids | tr '\n' ' ')" >&2
}

# worker-root.log is append-only, so report the LAST stamp of each service's own log.
report_build_stamps() {
  local head_sha entry stamp worker_log
  head_sha="$(git -C "$repo_root" rev-parse --short HEAD 2>/dev/null || echo '?')"
  worker_log="${log_dir}/worker.log"
  [[ -f "${log_dir}/worker-root.log" ]] && worker_log="${log_dir}/worker-root.log"
  echo "=== build stamps (expect g${head_sha}) ==="
  for entry in \
    "server:${log_dir}/server.log" \
    "reconciler:${log_dir}/reconciler.log" \
    "worker:${worker_log}"; do
    stamp="$(grep -h 'starting kdive' "${entry#*:}" 2>/dev/null | tail -1 |
      grep -oE 'g[0-9a-f]+ [(][a-z]+[)]' || true)"
    printf '  %-11s %s\n' "${entry%%:*}" "${stamp:-<no startup log line>}"
  done
}

# Returns 0 iff the host MCP server answers 401 (= up, auth required).
server_health() {
  local host port code
  host="${KDIVE_HTTP_HOST:-127.0.0.1}"
  port="${KDIVE_HTTP_PORT:-8000}"
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://${host}:${port}/mcp" || echo 000)"
  printf 'server http://%s:%s/mcp -> %s (401 = up, auth required)\n' "$host" "$port" "$code"
  [[ "$code" == "401" ]]
}

# libvirt daemon reachable. Sufficient as a libvirt signal: the provider needs no network/pool.
libvirt_ok() {
  virsh -c "$KDIVE_LIBVIRT_URI" list >/dev/null 2>&1
}

# The host prerequisites a local-libvirt provision actually needs. Returns 0 iff all are
# PRESENT (existence only — ownership/writability is the root worker's concern, not testable
# reliably as the invoking user). up.sh creates the dirs before calling this.
provision_prereqs_ok() {
  local rc=0 staging="${KDIVE_INSTALL_STAGING:-/var/lib/kdive/install}"
  command -v qemu-img >/dev/null 2>&1 || {
    echo "  MISSING: qemu-img on PATH (needed for rootfs overlays)" >&2
    rc=1
  }
  [[ -d "$KDIVE_ROOTFS_DIR" ]] || {
    echo "  MISSING: ${KDIVE_ROOTFS_DIR} (per-System qcow2 overlay dir)" >&2
    rc=1
  }
  [[ -d "$staging" ]] || {
    echo "  MISSING: ${staging} (KDIVE_INSTALL_STAGING)" >&2
    rc=1
  }
  return "$rc"
}

# Names of kdive-provisioned libvirt domains (kdive-<id>), one per line.
kdive_domains() {
  virsh -c "$KDIVE_LIBVIRT_URI" list --all --name 2>/dev/null | grep -E '^kdive-' || true
}
```

- [ ] **Step 2: Lint `lib.sh`**

Run: `shellcheck scripts/live-stack/lib.sh && shfmt -i 2 -d scripts/live-stack/lib.sh`
Expected: no output, exit 0. (`shellcheck` may warn SC2034 for vars used only by sourcing scripts — if so, add `# shellcheck disable=SC2034` above the affected declarations with the justification "consumed by sourcing scripts".)

- [ ] **Step 3: Verify `lib.sh` sources cleanly and exposes the interface**

Run:
```bash
bash -c 'source scripts/live-stack/lib.sh && echo "backends=${KDIVE_BACKEND_SERVICES[*]}" && declare -F daemon_pids stop_daemons report_build_stamps server_health libvirt_ok provision_prereqs_ok kdive_domains'
```
Expected: prints `backends=postgres minio minio-init oidc` followed by a `declare -f` line for each of the seven functions.

- [ ] **Step 4: Refactor `restart-stack.sh` to source `lib.sh`**

Replace the entire body of `scripts/live-stack/restart-stack.sh` (keep the existing top comment block, lines 1-23) from `set -euo pipefail` onward with:

```bash
set -euo pipefail

# Shared helpers (repo_root, py, log_dir, daemon_pids, stop_daemons, report_build_stamps,
# server_health). lib.sh resolves repo_root from its own location.
# shellcheck source=scripts/live-stack/lib.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
cd "$repo_root"

worker_as_root="${KDIVE_WORKER_AS_ROOT:-1}"
# The unprivileged account a root worker drops to for local builds; without it the root worker
# refuses the local build lane (deny-by-default, ADR-0214). Defaults to whoever runs this script.
build_user="${KDIVE_BUILD_USER:-$(id -un)}"

[[ -x "$py" ]] || {
  echo "no venv python at ${py}; run 'just setup' first" >&2
  exit 1
}
mkdir -p "$log_dir"

start_user_daemon() {
  local name="$1"
  setsid nohup "$py" -m kdive "$name" >"${log_dir}/${name}.log" 2>&1 </dev/null &
}

start_worker() {
  local kernel_src="${KDIVE_KERNEL_SRC:-${HOME}/src/linux}"
  if [[ "$worker_as_root" == "1" && "$(id -un)" != "root" ]]; then
    # The worker needs root (install staging + libvirt/VM ops). Export KDIVE_KERNEL_SRC *before*
    # sourcing env.sh so env.sh honors it verbatim instead of defaulting to ${HOME}/src/linux,
    # which under sudo (HOME=/root) would silently point at a nonexistent /root/src/linux.
    sudo bash -c "cd '${repo_root}' \
      && export KDIVE_KERNEL_SRC='${kernel_src}' KDIVE_BUILD_USER='${build_user}' \
      && source scripts/live-stack/env.sh \
      && setsid nohup '${py}' -m kdive worker >>'${log_dir}/worker-root.log' 2>&1 </dev/null &"
  else
    KDIVE_KERNEL_SRC="$kernel_src" start_user_daemon worker
  fi
}

stop_daemons

echo "starting kdive stack @ $(git -C "$repo_root" rev-parse --short HEAD 2>/dev/null || echo '?') ..."
# shellcheck disable=SC1091 # repo-relative env script resolved from this script's location
source scripts/live-stack/env.sh
start_user_daemon server
start_user_daemon reconciler
start_worker

sleep 5

echo
echo "=== running kdive daemons ==="
# $1 is the numeric PID column, so this both matches real daemons and drops the awk self-line
# and any header. (Do NOT test $2 — that is the username, never numeric.)
ps -eo pid,user,lstart,args | awk -v re="$_daemon_match" '$0 ~ re && $1 ~ /^[0-9]+$/'
echo
report_build_stamps
echo
server_health || true
```

- [ ] **Step 5: Lint the refactored `restart-stack.sh`**

Run: `shellcheck scripts/live-stack/restart-stack.sh && shfmt -i 2 -d scripts/live-stack/restart-stack.sh`
Expected: no output, exit 0.

- [ ] **Step 6: Behavior-preserving smoke (live, run on this host)**

Run via the `!` prefix (needs sudo for the root worker): `! scripts/live-stack/restart-stack.sh`
Expected: prints the daemon table, `=== build stamps (expect g<HEAD>) ===` with all three on `g<HEAD>`, and `server ... -> 401`.

- [ ] **Step 7: Commit**

```bash
git add scripts/live-stack/lib.sh scripts/live-stack/restart-stack.sh
git commit -m "refactor(live-stack): extract shared lib.sh from restart-stack.sh"
```

---

### Task 2: `status.sh` read-only health report

**Files:**
- Create: `scripts/live-stack/status.sh`

**Interfaces:**
- Consumes: everything `lib.sh` produces; sources `env.sh` for `KDIVE_DATABASE_URL`/`KDIVE_HTTP_*`.
- Produces: `status.sh` (invoked by `up.sh` step 7). Exit code is informational (always 0); it reports, it does not gate.

- [ ] **Step 1: Write `scripts/live-stack/status.sh`**

```bash
#!/usr/bin/env bash
#
# Read-only health report for the local kdive infrastructure. No side effects.
# Usage: scripts/live-stack/status.sh
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/live-stack/lib.sh
source "${here}/lib.sh"
# shellcheck disable=SC1091 # repo-relative env script
source "${here}/env.sh"
cd "$repo_root"

echo "=== compose backends + obs ==="
docker compose ps --format 'table {{.Service}}\t{{.Status}}' \
  "${KDIVE_BACKEND_SERVICES[@]}" prometheus grafana 2>/dev/null || echo "  (docker compose unavailable)"

echo
echo "=== host daemons ==="
# $1 is the numeric PID column (drops the awk self-line + header); $2 is the username.
ps -eo pid,user,args | awk -v re="$_daemon_match" '$0 ~ re && $1 ~ /^[0-9]+$/' || true
echo
report_build_stamps

echo
echo "=== app health ==="
server_health || true

echo
echo "=== database ==="
if "$py" - <<PY 2>/dev/null; then
import os, sys
import psycopg

try:
    psycopg.connect(os.environ["KDIVE_DATABASE_URL"], connect_timeout=5).close()
except Exception as exc:  # noqa: BLE001 - status probe reports, does not raise
    print(f"  UNREACHABLE: {exc}")
    sys.exit(1)
print("  reachable")
PY
  :
else
  echo "  UNREACHABLE (see KDIVE_DATABASE_URL)"
fi

echo
echo "=== libvirt (${KDIVE_LIBVIRT_URI}) ==="
if libvirt_ok; then
  echo "  daemon: reachable"
else
  echo "  daemon: UNREACHABLE"
fi
if provision_prereqs_ok; then
  echo "  provision prereqs: qemu-img + ${KDIVE_ROOTFS_DIR} OK"
else
  echo "  provision prereqs: INCOMPLETE (see MISSING lines above)"
fi
```

- [ ] **Step 2: Lint**

Run: `shellcheck scripts/live-stack/status.sh && shfmt -i 2 -d scripts/live-stack/status.sh`
Expected: no output, exit 0.

- [ ] **Step 3: Run it (live, on this host)**

Run: `scripts/live-stack/status.sh`
Expected: five sections (compose backends, host daemons + build stamps, app health, database, libvirt). With backends up it shows postgres/minio/oidc `Up`, daemons on `g<HEAD>`, `server ... -> 401`, `database ... reachable`, and the libvirt lines.

- [ ] **Step 4: Commit**

```bash
git add scripts/live-stack/status.sh
git commit -m "feat(live-stack): add read-only status.sh health report"
```

---

### Task 3: Grafana compose service + provisioning

**Files:**
- Modify: `docker-compose.yml` (insert after the `prometheus` block, before `volumes:`)
- Create: `deploy/compose/grafana/provisioning/datasources/prometheus.yml`
- Create: `deploy/compose/grafana/provisioning/dashboards/kdive.yml`

**Interfaces:**
- Consumes: existing `prometheus` service (scrape source) and `deploy/grafana/kdive-overview.json` (dashboard).
- Produces: a `grafana` service reachable at `http://localhost:3000`, auto-loading the kdive-overview dashboard wired to the provisioned default Prometheus datasource.

- [ ] **Step 1: Create the datasource provisioning file**

`deploy/compose/grafana/provisioning/datasources/prometheus.yml`:
```yaml
# Grafana datasource provisioning for the compose obs profile. The kdive-overview dashboard's
# ${datasource} template variable resolves to the default datasource, so this MUST be isDefault.
apiVersion: 1
datasources:
  - name: Prometheus
    uid: prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true
    editable: false
```

- [ ] **Step 2: Create the dashboard provider file**

`deploy/compose/grafana/provisioning/dashboards/kdive.yml`:
```yaml
# File-based dashboard provider. Loads every dashboard JSON mounted under the options.path dir;
# kdive-overview.json is mounted there read-only by the compose grafana service.
apiVersion: 1
providers:
  - name: kdive
    type: file
    disableDeletion: true
    allowUiUpdates: false
    options:
      path: /var/lib/grafana/dashboards
      foldersFromFilesStructure: false
```

- [ ] **Step 3: Add the `grafana` service to `docker-compose.yml`**

Insert immediately after the `prometheus` service block (after its `ports:` mapping, line ~232) and before the top-level `volumes:` key:

```yaml
  # Grafana for the kdive-overview dashboard (deploy/grafana/README.md). Behind the `obs`
  # profile alongside prometheus. Anonymous access is enabled for a frictionless LOCAL dev box
  # only — do not reuse this posture off localhost. Datasource + dashboard are provisioned, so
  # state is declarative on every start (no named volume).
  grafana:
    image: grafana/grafana:13.0.3
    profiles: ["obs"]
    depends_on:
      - prometheus
    environment:
      GF_AUTH_ANONYMOUS_ENABLED: "true"
      GF_AUTH_ANONYMOUS_ORG_ROLE: Admin
      GF_USERS_ALLOW_SIGN_UP: "false"
    volumes:
      - ./deploy/compose/grafana/provisioning:/etc/grafana/provisioning:ro
      - ./deploy/grafana/kdive-overview.json:/var/lib/grafana/dashboards/kdive-overview.json:ro
    ports:
      - "3000:3000"
```

- [ ] **Step 4: Validate the compose file**

Run: `docker compose config >/dev/null && echo OK`
Expected: `OK` (no YAML/schema errors).

- [ ] **Step 5: Bring up Grafana and verify it renders with live data (live, on this host)**

Run:
```bash
docker compose --profile obs up -d prometheus grafana
sleep 5
curl -s -o /dev/null -w 'grafana health: %{http_code}\n' http://localhost:3000/api/health
curl -s 'http://localhost:3000/api/search?query=kdive' | grep -o '"title":"[^"]*"' | head
```
Expected: `grafana health: 200` and a search hit containing the kdive-overview dashboard title (confirms the dashboard provisioned). Open `http://localhost:3000` → the kdive-overview dashboard loads with the Prometheus datasource selected (panels populate once traffic flows).

- [ ] **Step 6: Commit**

```bash
git add docker-compose.yml deploy/compose/grafana/
git commit -m "feat(compose): add grafana to obs profile with provisioned datasource + dashboard"
```

---

### Task 5: `up.sh` full bring-up

> **Execution order:** build Task 4 (`down.sh`) before this task — `up.sh` calls `down.sh` on the `--reset-db` path and in its own smoke test. (This section appears before Task 4 in the file for readability; execute by task number.)

**Files:**
- Create: `scripts/live-stack/up.sh`

**Interfaces:**
- Consumes: `lib.sh`, `env.sh`, `apply-migrations.sh`, `restart-stack.sh`, `status.sh`, `down.sh` (Task 4, built first).
- Produces: `up.sh [--reset-db] [--skip-obs]`. `--reset-db` runs `down.sh --wipe --yes` then proceeds; `--skip-obs` sets `KDIVE_SKIP_OBS=1`.

- [ ] **Step 1: Write `scripts/live-stack/up.sh`**

```bash
#!/usr/bin/env bash
#
# Bring up the WHOLE local kdive infrastructure, idempotently and in order:
#   backends (compose) -> migrations (host) -> libvirt -> host processes -> status.
# Run via the `!` prefix; it self-elevates with sudo for libvirt and the root worker.
#
# Usage:
#   scripts/live-stack/up.sh                 full bring-up
#   scripts/live-stack/up.sh --reset-db      wipe the DB first (recovery from migration drift)
#   scripts/live-stack/up.sh --skip-obs      skip prometheus/grafana
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/live-stack/lib.sh
source "${here}/lib.sh"
# shellcheck disable=SC1091 # repo-relative env script
source "${here}/env.sh"
cd "$repo_root"

reset_db=0
skip_obs="${KDIVE_SKIP_OBS:-0}"
for arg in "$@"; do
  case "$arg" in
    --reset-db) reset_db=1 ;;
    --skip-obs) skip_obs=1 ;;
    *)
      echo "unknown argument: $arg (accepts --reset-db, --skip-obs)" >&2
      exit 2
      ;;
  esac
done

banner() { printf '\n=== %s ===\n' "$1"; }

if [[ "$reset_db" == "1" ]]; then
  banner "reset-db (down --wipe)"
  "${here}/down.sh" --wipe --yes
fi

banner "preflight"
[[ -x "$py" ]] || {
  echo "no venv python at ${py}; run 'just setup' first" >&2
  exit 1
}
command -v docker >/dev/null 2>&1 || {
  echo "docker not on PATH" >&2
  exit 1
}

banner "reconcile app tier (never run the kdive:dev containers)"
# A subset `up -d` of the backends does not create the app tier, but a previously running
# compose `server` would hold port 8000 against the host process. Remove any such container.
docker compose rm -sf migrate server worker reconciler >/dev/null 2>&1 || true

banner "backends"
docker compose up -d "${KDIVE_BACKEND_SERVICES[@]}"
if [[ "$skip_obs" != "1" ]]; then
  docker compose --profile obs up -d prometheus grafana
fi
echo "waiting for postgres to report healthy ..."
for _ in {1..30}; do
  [[ "$(docker compose ps postgres --format '{{.Health}}' 2>/dev/null)" == "healthy" ]] && break
  sleep 1
done
[[ "$(docker compose ps postgres --format '{{.Health}}' 2>/dev/null)" == "healthy" ]] || {
  echo "postgres did not become healthy in time" >&2
  exit 1
}

banner "migrations (host checkout = authoritative)"
if ! bash "${here}/apply-migrations.sh"; then
  echo >&2
  echo "migration step failed. If this is the ADR-0015 immutable-migration guard (the DB's" >&2
  echo "applied history diverges from this checkout), recover with:" >&2
  echo "    scripts/live-stack/up.sh --reset-db" >&2
  exit 1
fi

banner "libvirt"
# The provider uses user-mode SLIRP networking (no libvirt network), so only virtqemud is
# needed — do NOT manage virtnetworkd. virtqemud is socket-activated, so `libvirt_ok` (a
# `virsh list`) activates it on connect; gate on that, not `systemctl is-active` (which reports
# the *service* inactive on a healthy socket-activated host and would re-sudo every run).
if ! libvirt_ok; then
  echo "libvirt unreachable; enabling virtqemud.socket (sudo) ..."
  sudo systemctl enable --now virtqemud.socket
fi
libvirt_ok || {
  echo "libvirt daemon not reachable at ${KDIVE_LIBVIRT_URI}" >&2
  exit 1
}
# Create the provision dirs (idempotent) so a clean host isn't gated on dirs nothing made.
# The root worker owns/writes them at provision time; existence is all up.sh requires.
sudo mkdir -p "$KDIVE_ROOTFS_DIR" "${KDIVE_INSTALL_STAGING:-/var/lib/kdive/install}"
provision_prereqs_ok || {
  echo "libvirt reachable but provision prerequisites are missing (see MISSING lines)" >&2
  exit 1
}

banner "host processes"
"${here}/restart-stack.sh"

banner "status"
"${here}/status.sh"
```

- [ ] **Step 2: Lint**

Run: `shellcheck scripts/live-stack/up.sh && shfmt -i 2 -d scripts/live-stack/up.sh`
Expected: no output, exit 0.

- [ ] **Step 3: Full live bring-up from a torn-down state (live, on this host)**

Run: `! scripts/live-stack/down.sh && scripts/live-stack/up.sh`
Expected: banners for each layer in order; ends with the `status.sh` report showing backends up, daemons on `g<HEAD>`, `server ... -> 401`, database reachable, libvirt reachable + provision prereqs OK.

- [ ] **Step 4: Verify it provisions (the original motivating task) (live, on this host)**

After `up.sh`, drive a local-libvirt provision (mint a token + call the MCP provision flow per `docs/operating/providers/local-libvirt-walkthrough.md`).
Expected: a `kdive-<id>` domain reaches a `ready` System — confirms the whole stack is functional, not just up.

- [ ] **Step 5: Commit**

```bash
git add scripts/live-stack/up.sh
git commit -m "feat(live-stack): add up.sh full-infrastructure orchestrator"
```

---

### Task 4: `down.sh` teardown

> **Execution order:** build this before Task 5 (`up.sh`), which depends on it. (This section appears after Task 5 in the file; execute by task number.)

**Files:**
- Create: `scripts/live-stack/down.sh`

**Interfaces:**
- Consumes: `lib.sh` (`stop_daemons`, `kdive_domains`, `KDIVE_LIBVIRT_URI`, `KDIVE_ROOTFS_DIR`).
- Produces: `down.sh [--wipe] [--yes]`. `--wipe` adds `compose down -v` and reaps `kdive-*` domains + overlays; `--yes` skips the confirmation prompt (used by `up.sh --reset-db`).

- [ ] **Step 1: Write `scripts/live-stack/down.sh`**

```bash
#!/usr/bin/env bash
#
# Tear down the local kdive infrastructure: stop host processes + compose backends.
# Plain teardown keeps state (Postgres volume + any running kdive-* domains). `--wipe` is a
# full reset: it drops the Postgres volume AND reaps kdive-provisioned libvirt domains and
# their qcow2 overlays (these live outside compose, so a DB wipe alone would orphan them).
# libvirt itself is left enabled and running (host service; not cycled per teardown).
#
# Usage:
#   scripts/live-stack/down.sh            stop the stack, keep state
#   scripts/live-stack/down.sh --wipe     also wipe DB + reap kdive domains/overlays
#   scripts/live-stack/down.sh --wipe --yes   skip the confirmation prompt
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/live-stack/lib.sh
source "${here}/lib.sh"
cd "$repo_root"

wipe=0
assume_yes=0
for arg in "$@"; do
  case "$arg" in
    --wipe) wipe=1 ;;
    --yes) assume_yes=1 ;;
    *)
      echo "unknown argument: $arg (accepts --wipe, --yes)" >&2
      exit 2
      ;;
  esac
done

if [[ "$wipe" == "1" && "$assume_yes" != "1" ]]; then
  echo "WARNING: --wipe drops the Postgres volume and destroys all kdive-* libvirt domains" >&2
  echo "and their overlay disks. This is irreversible." >&2
  # An interactive prompt needs a tty; under the agent `!` prefix (or any piped stdin) `read`
  # gets EOF and would silently abort. Require --yes instead of hanging/aborting confusingly.
  if [[ ! -t 0 ]]; then
    echo "non-interactive stdin: re-run as 'down.sh --wipe --yes' to confirm" >&2
    exit 1
  fi
  read -r -p "Type 'wipe' to proceed: " confirm
  [[ "$confirm" == "wipe" ]] || {
    echo "aborted"
    exit 1
  }
fi

echo "=== stopping host processes ==="
stop_daemons

echo "=== stopping compose backends + obs ==="
if [[ "$wipe" == "1" ]]; then
  docker compose --profile obs down -v
else
  docker compose --profile obs down
fi

if [[ "$wipe" == "1" ]]; then
  echo "=== reaping kdive-* libvirt domains + overlays ==="
  while read -r dom; do
    [[ -n "$dom" ]] || continue
    echo "  destroying ${dom}"
    sudo virsh -c "$KDIVE_LIBVIRT_URI" destroy "$dom" 2>/dev/null || true
    sudo virsh -c "$KDIVE_LIBVIRT_URI" undefine "$dom" 2>/dev/null || true
  done < <(kdive_domains)
  sudo rm -f "${KDIVE_ROOTFS_DIR}"/*-overlay.qcow2 2>/dev/null || true
fi

echo "done"
```

- [ ] **Step 2: Lint**

Run: `shellcheck scripts/live-stack/down.sh && shfmt -i 2 -d scripts/live-stack/down.sh`
Expected: no output, exit 0.

- [ ] **Step 3: Plain teardown keeps state (live, on this host)**

Run: `scripts/live-stack/down.sh` then `docker volume ls | grep -c kdive`
Expected: daemons stopped, backends down, but the kdive Postgres named volume still listed (count ≥ 1). A subsequent `up.sh` retains prior DB rows.

- [ ] **Step 4: `--wipe` produces a clean slate (live, on this host)**

In a real terminal: `scripts/live-stack/down.sh --wipe` (type `wipe` at the prompt). Through the
agent `!` prefix (non-tty), use `! scripts/live-stack/down.sh --wipe --yes` instead. Then `scripts/live-stack/up.sh`.
Expected: the Postgres volume is recreated empty (migrations re-apply from scratch), no `kdive-*` domains remain (`virsh -c qemu:///system list --all --name | grep kdive` → empty), and no `*-overlay.qcow2` files remain under `/var/lib/kdive/rootfs`.

- [ ] **Step 5: Commit**

```bash
git add scripts/live-stack/down.sh
git commit -m "feat(live-stack): add down.sh teardown with --wipe domain/overlay reaping"
```

---

### Task 6: Automated invariant guard

**Files:**
- Create: `tests/live_stack/test_up_invariants.py`

**Interfaces:**
- Consumes: the text of `scripts/live-stack/up.sh` (Task 4).
- Produces: a pytest module guarding the two invariants that must never regress.

- [ ] **Step 1: Write the failing test**

`tests/live_stack/test_up_invariants.py`:
```python
"""Guard the invariants that keep up.sh from fighting the host app tier.

up.sh must never start the kdive:dev compose app tier (migrate/server/worker/reconciler) —
the host processes own that tier and the host apply-migrations.sh is the authoritative
migrator. These are text-level guards because the scripts are not import-testable.
"""

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_UP = _REPO_ROOT / "scripts" / "live-stack" / "up.sh"
_APP_TIER = ("migrate", "server", "worker", "reconciler")
# Any `compose ... up ...` invocation, regardless of intervening flags (e.g. `--profile obs`).
_COMPOSE_UP = re.compile(r"compose\b.*\bup\b")


def test_up_reconciles_app_tier_before_start() -> None:
    text = _UP.read_text()
    assert "rm -sf migrate server worker reconciler" in text


def test_up_never_starts_the_app_tier() -> None:
    text = _UP.read_text()
    for line in text.splitlines():
        # Match `compose up` even with flags between (`compose --profile obs up`); a naive
        # "compose up" substring check would miss the profile-flag form and let the very
        # regression this guard exists to catch slip through.
        if not _COMPOSE_UP.search(line):
            continue
        for svc in _APP_TIER:
            assert not re.search(rf"\b{svc}\b", line), f"up.sh starts app-tier service in: {line!r}"


def test_up_uses_the_canonical_backend_list() -> None:
    text = _UP.read_text()
    assert "KDIVE_BACKEND_SERVICES" in text
```

- [ ] **Step 2: Run it to verify it passes against the real up.sh**

Run: `uv run pytest tests/live_stack/test_up_invariants.py -v`
Expected: 3 passed. (up.sh from Task 4 already satisfies the invariants — this is a regression guard.)

- [ ] **Step 3: Verify the guard actually catches a regression**

Temporarily edit a copy and confirm BOTH regression forms fail (the plain and the profile-flag
form the naive substring check used to miss):
- change a backends line to `docker compose up -d server` → expect FAIL.
- change it to `docker compose --profile obs up -d server` → expect FAIL (this is the form the
  old `"compose up" in line` check let slip).
Revert after each.
Run: `uv run pytest tests/live_stack/test_up_invariants.py::test_up_never_starts_the_app_tier -v`
Expected: FAIL while broken (both forms), PASS after revert. (Confirms the guard has teeth, per the "verify tests catch failures" standard.)

- [ ] **Step 4: Commit**

```bash
git add tests/live_stack/test_up_invariants.py
git commit -m "test(live-stack): guard up.sh never starts the compose app tier"
```

---

### Task 7: Document the lifecycle scripts

**Files:**
- Modify: `deploy/compose/README.md`

**Interfaces:**
- Consumes: the four scripts + grafana service from prior tasks.
- Produces: operator-facing docs for the local lifecycle.

- [ ] **Step 1: Append a "Local lifecycle scripts" section to `deploy/compose/README.md`**

Add at the end of the file:
```markdown
## Local lifecycle scripts (this dev host)

For a hand-rolled local stack (host-run server/reconciler/worker against compose backends),
use the lifecycle scripts under `scripts/live-stack/`. They self-elevate with `sudo` for the
root worker and libvirt, so run them via the `!` prefix in the agent or directly in a shell:

- `up.sh` — full bring-up in order: backends → host migrations → libvirt → host processes →
  status. `--skip-obs` omits prometheus/grafana; `--reset-db` wipes the DB first (recovery from
  migration drift — see below).
- `down.sh` — stop host processes + compose backends, keeping state. `--wipe` is a full reset:
  drops the Postgres volume and reaps `kdive-*` libvirt domains + their `/var/lib/kdive/rootfs`
  overlays.
- `status.sh` — read-only per-layer health (backends, host daemons + build stamps, server,
  database, libvirt + provision prereqs).

The scripts never start the compose `kdive:dev` app tier (`migrate`/`server`/`worker`/
`reconciler`); the host processes own that tier and `apply-migrations.sh` (current checkout) is
the authoritative migrator.

**Migration drift:** the ADR-0015 immutable-migration guard fires when the persisted DB's
applied-migration history diverges from your checkout (e.g. after switching branches). `up.sh`
aborts at the migrations step with a clear message; recover with `up.sh --reset-db`.

**Grafana:** `up.sh` brings up Grafana (obs profile) at http://localhost:3000 with the
kdive-overview dashboard auto-provisioned against Prometheus. Anonymous access is enabled for
local convenience only.
```

- [ ] **Step 2: Verify the doc renders and links are valid**

Run: `rg -n "up.sh|down.sh|status.sh" deploy/compose/README.md`
Expected: matches in the new section (sanity check the section landed).

- [ ] **Step 3: Commit**

```bash
git add deploy/compose/README.md
git commit -m "docs(compose): document the local-stack lifecycle scripts + grafana"
```

---

## Self-Review

**Spec coverage:**
- `lib.sh` (shared funcs + backend list) → Task 1. ✓
- `restart-stack.sh` refactor → Task 1. ✓
- `up.sh` (preflight, app-tier reconcile, backends+obs, migrations w/ drift remediation, libvirt + prereqs, host processes, status) → Task 4. ✓
- `down.sh` (`--wipe` reaps domains/overlays) → Task 5. ✓
- `status.sh` (distinct libvirt vs provision-prereqs lines) → Task 2. ✓
- Grafana in compose + provisioning + isDefault datasource → Task 3. ✓
- "What the provider actually needs" (no libvirt net/pool; qemu-img + rootfs) → encoded in `lib.sh` `provision_prereqs_ok` + `up.sh` libvirt step. ✓
- Migration-drift handling → Task 4 step 1 + Task 7 docs. ✓
- Invariant guard (grep test) → Task 6. ✓
- Grafana renders with live data acceptance → Task 3 step 5. ✓
- Testing: shellcheck/shfmt per script, `docker compose config`, live smoke, drift path → distributed across tasks. ✓

**Placeholder scan:** No TBD/TODO; every code step has complete content. ✓

**Type/name consistency:** `KDIVE_BACKEND_SERVICES`, `daemon_pids`, `stop_daemons`, `report_build_stamps`, `server_health`, `libvirt_ok`, `provision_prereqs_ok`, `kdive_domains`, `_daemon_match` defined in Task 1 and used identically in Tasks 2/4/5. `down.sh --wipe --yes` defined in Task 5 and called with those exact flags by `up.sh --reset-db` in Task 4. ✓

**Decisions defaulted (flag if you disagree):**
- Grafana auth = anonymous, org role Admin (local-only, frictionless). Documented as localhost-only.
- `--reset-db` = explicit opt-in flag; `up.sh` never auto-wipes on drift, only suggests it.

**Adversarial-review fixes applied (verified):**
- Daemon-status awk filters on `$1` (numeric PID), not `$2` (username) — table no longer renders empty.
- libvirt step manages only `virtqemud` (provider uses user-mode SLIRP) and gates on `libvirt_ok`, not `systemctl is-active` — no spurious `sudo` on every run (confirmed: `virtnetworkd.service` is inactive while its socket is active on a healthy host).
- Invariant guard matches `compose … up` across intervening flags (regex), closing the `--profile obs up -d server` blind spot; logic validated (no false positives, both regression forms caught).
- `up.sh` `sudo mkdir -p`s the rootfs + install-staging dirs before asserting; `provision_prereqs_ok` checks existence honestly (incl. `KDIVE_INSTALL_STAGING`) and no longer over-claims writability.
- `down.sh --wipe` rejects non-tty stdin with a `--yes` hint instead of EOF-aborting under the `!` prefix.
