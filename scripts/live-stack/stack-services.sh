#!/usr/bin/env bash
#
# Bring up the local kdive infrastructure, idempotently and in order:
#   backends (compose) -> migrations (host) -> libvirt -> host processes -> status.
# Run as the provisioned lifecycle-control operator (never UID 0). Libvirt bring-up is
# unprivileged on a provisioned host (the dedicated session daemon, #2032); only a bare dev host
# still elevates, via sudo, to socket-activate the system daemon.
#
# `--stage backends` stops after migrations: backends healthy, artifacts bucket created and
# verified, schema migrated. That is what `just stack-backends` runs, so there is one
# implementation and one backend readiness contract rather than two (ADR-0655).
#
# Usage:
#   scripts/live-stack/stack-services.sh                 full bring-up
#   scripts/live-stack/stack-services.sh --stage backends   backends + bucket + schema, then stop
#   scripts/live-stack/stack-services.sh --reset-db      wipe the DB first (recovery from migration drift)
#   scripts/live-stack/stack-services.sh --skip-obs      skip prometheus/grafana
#   scripts/live-stack/stack-services.sh --skip-libvirt  backends + host processes only (no VM provisioning)
#
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/live-stack/lib.sh
source "${here}/lib.sh"
# shellcheck disable=SC1091 # repo-relative env script
source "${here}/env.sh"
cd "$repo_root"

reset_db=0
skip_obs="${KDIVE_SKIP_OBS:-0}"
skip_libvirt=0
stage="services"
# A `while`/`shift` loop, not `for arg in "$@"`: that word list is expanded once before the body
# runs, so a shift inside it cannot consume `--stage`'s value token.
while [[ $# -gt 0 ]]; do
  case "$1" in
  --reset-db) reset_db=1 ;;
  --skip-obs) skip_obs=1 ;;
  --skip-libvirt) skip_libvirt=1 ;;
  --stage)
    shift
    stage="${1:-}"
    case "$stage" in
    backends | services) ;;
    *)
      echo "unknown --stage '${stage}': expected 'backends' or 'services'" >&2
      exit 2
      ;;
    esac
    ;;
  *)
    echo "unknown argument: $1 (accepts --stage, --reset-db, --skip-obs, --skip-libvirt)" >&2
    exit 2
    ;;
  esac
  shift
done
# Reject only the flags whose phases the backends stage cannot reach. --skip-obs is deliberately
# absent: it defaults from the documented KDIVE_SKIP_OBS, so rejecting it would fail
# `KDIVE_SKIP_OBS=1 just stack-backends` over a flag the operator never passed. It is inert here.
if [[ "$stage" == "backends" ]]; then
  for flag in reset_db skip_libvirt; do
    if [[ "${!flag}" == "1" ]]; then
      echo "--stage backends does not reach the phase --${flag//_/-} controls" >&2
      exit 2
    fi
  done
fi
if ((EUID == 0)); then
  echo "stack-services.sh must run as the provisioned lifecycle-control operator, not UID 0" >&2
  exit 1
fi

banner() { printf '\n=== %s ===\n' "$1"; }

if [[ "$reset_db" == "1" ]]; then
  banner "reset-db (down --wipe)"
  "${here}/stack-down.sh" --wipe --yes
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

# Destructive against the containerized tier, and it exists because host processes and a compose
# `server` contend for port 8000 — a services concern. A backends-only bring-up must not run it.
if [[ "$stage" == "services" ]]; then
  banner "reconcile app tier (never run the kdive:dev containers)"
  # A subset `up -d` of the backends does not create the app tier, but a previously running
  # compose `server` would hold port 8000 against the host process. Remove any such container.
  docker compose rm -sf migrate server worker reconciler >/dev/null 2>&1 || true
fi

banner "backends"
live_stack_backends_up

banner "migrations (host checkout = authoritative)"
if ! bash "${here}/apply-migrations.sh"; then
  echo >&2
  echo "migration step failed. If this is the ADR-0015 immutable-migration guard (the DB's" >&2
  echo "applied history diverges from this checkout), recover with:" >&2
  echo "    scripts/live-stack/stack-services.sh --reset-db" >&2
  exit 1
fi

if [[ "$stage" == "backends" ]]; then
  banner "backends stage complete"
  echo "Backends healthy, bucket verified, schema migrated."
  echo "App tier, for IN-NETWORK clients: just compose-up"
  echo "For libvirt and the host processes: scripts/live-stack/stack-services.sh"
  echo "  (compose containers get a different OIDC issuer identity than a host-minted token"
  echo "   carries -> 401, and no /dev/kvm or libvirt socket -> no local VM. See the runbook.)"
  echo "MCP URL: http://127.0.0.1:8000/mcp"
  echo "Full runbook: docs/operating/runbooks/live-stack.md"
  exit 0
fi

# Observability is a services phase: the backends-only path never started prometheus, and
# `just stack-backends` must not either. It sits after the stage gate for that reason.
if [[ "$skip_obs" != "1" ]]; then
  banner "observability"
  # Bring prometheus up on its own first: it publishes ppc64le and is the metrics store, so a
  # grafana failure (missing manifest, bad tag, registry outage) must never abort it. Grafana
  # ships no ppc64le manifest (ADR-0356 accept-gap), so skip it outright on POWER — otherwise its
  # pull prints a "no matching manifest" error every run — and start it best-effort elsewhere. An
  # operator runs grafana on their own workstation pointed at this host's published prometheus
  # port (http://<this-host>:9090). See issue #1261.
  host_arch="$(uname -m 2>/dev/null || true)"
  if ! docker compose --profile obs up -d prometheus; then
    echo "WARNING: prometheus (metrics store) failed to start; essential stack continues" >&2
  fi
  if ! grafana_supports_arch "$host_arch"; then
    echo "NOTE: skipping grafana on ${host_arch} (no upstream manifest; ADR-0356 / #1261); prometheus is up at :9090" >&2
  elif ! docker compose --profile obs up -d grafana; then
    echo "WARNING: grafana failed to start; prometheus continues" >&2
  fi
fi

banner "runtime-role bootstrap"
# The compose app tier gates on the role-bootstrap one-shot (its depends_on in
# docker-compose.yml), but this path runs the app tier as HOST processes and the backend set
# above excludes the one-shot — so without running it here the four runtime login members never
# come to exist and every daemon and installed worker fails closed with dependency_unavailable
# at fleet start (#2036). The one-shot is idempotent (guarded CREATE plus revoke/grant
# convergence), so bring-up runs it on every pass while local bootstrap is enabled;
# KDIVE_LOCAL_ROLE_BOOTSTRAP=0 keeps the external-provisioning contract: no database mutation,
# the operator supplies every member. `env -u` drops env.sh's host-facing migration DSN
# (localhost), which is unreachable from inside the compose network, so the one-shot
# interpolates its own container-internal postgres:5432 default.
if [[ "${KDIVE_LOCAL_ROLE_BOOTSTRAP:-1}" == "1" ]]; then
  if ! env -u KDIVE_MIGRATION_DATABASE_URL \
    docker compose run --rm --no-deps role-bootstrap; then
    echo >&2
    echo "runtime-role bootstrap failed; the runtime login members are missing" >&2
    exit 1
  fi
else
  echo "KDIVE_LOCAL_ROLE_BOOTSTRAP=0; using externally provisioned login members"
fi

if [[ "$skip_libvirt" != "1" ]]; then
  banner "libvirt"
  # The provider uses user-mode SLIRP networking (no libvirt network), so only the qemu daemon is
  # needed — do NOT manage virtnetworkd. Gate on `libvirt_ok` (a `virsh list`) and `nodedev_ok`
  # (a `virsh nodedev-list`), not `systemctl is-active`, which reports the *service* inactive on
  # a healthy socket-activated host. Both checks matter here (#2401): under the modular daemon
  # model `virsh list` succeeds via virtqemud alone, so a host with virtqemud already enabled but
  # virtnodedevd never enabled would otherwise skip this whole remediation block.
  if ! libvirt_ok || ! nodedev_ok; then
    if [[ "$KDIVE_LIBVIRT_URI" == *"live-libvirt"* ]]; then
      # Provisioned-runner recovery (#2032): the dedicated session endpoint is down (fresh boot,
      # reprovision lag). Start the OPERATOR-OWNED session daemon as the invoking user — the same
      # daemon shape the live_vm_host role provisions and keeps boot-persistent via its systemd
      # --user unit. No sudo to start it: the runner service account has none, virtqemud does not
      # exist on the Debian-family runner, and degrading to qemu:///system would hit the
      # root-readback wall (ADR-0223) anyway. If the daemon cannot be started non-interactively,
      # die loud naming the missing paths instead of failing later with a confusing URI error.
      echo "libvirt unreachable at ${KDIVE_LIBVIRT_URI}; starting the dedicated session daemon ..."
      ensure_session_libvirtd || {
        echo "dedicated session daemon could not be started; refusing to fall back to a system daemon" >&2
        exit 1
      }
      if [[ "$KDIVE_LIBVIRT_URI" == *virtqemud-sock ]]; then
        # #2503: the modular virtqemud session daemon proxies node-device queries to the *system*
        # virtnodedevd socket -- there is no session counterpart -- and
        # docs/design/2026-09-09-ppc64le-emulated-power-live-proof-2383-proof-record.md:284-286
        # records the exact connection failure. Enable that system unit here too, mirroring the
        # bare-host branch below.
        # Best-effort: the provisioned CI runner has no sudo (#2032) and must not be blocked by
        # this, so tolerate a failed enable exactly like the bare-host branch's own `|| true`. The
        # Debian-family session daemon is the monolithic libvirtd, which answers node-device
        # queries itself (lib.sh's nodedev_ok comment), so it never takes this branch.
        echo "modular session daemon selected; enabling virtnodedevd.socket (sudo, best-effort) ..."
        sudo systemctl enable --now virtnodedevd.socket || true
      fi
    else
      # Bare dev host (qemu:///system default): the system daemon is socket-activated, so enable
      # --now plus the re-checks below are enough. Onboarding's resource discovery also needs
      # virtnodedevd (#2401), so enable it alongside virtqemud rather than leaving it for
      # discovery to crash on later. `|| true`: under `set -e` a partial two-unit enable failure
      # would otherwise abort here before either named-unit re-check below runs, losing the
      # specific diagnostic to systemd's own (unit-naming, but less actionable) error text.
      echo "libvirt or virtnodedevd unreachable; enabling virtqemud.socket + virtnodedevd.socket (sudo) ..."
      sudo systemctl enable --now virtqemud.socket virtnodedevd.socket || true
    fi
  fi
  libvirt_ok || {
    echo "libvirt daemon not reachable at ${KDIVE_LIBVIRT_URI}" >&2
    exit 1
  }
  # #2401: a monolithic libvirtd (the session-daemon recovery path) answers node-device queries
  # itself, but the modular system daemon needs virtnodedevd running separately — fail here,
  # naming the unit, instead of letting onboarding's resource discovery crash on it later.
  nodedev_ok || {
    echo "virtnodedevd not reachable at ${KDIVE_LIBVIRT_URI}; enable virtnodedevd.socket" >&2
    exit 1
  }
  # Create the provision dirs (idempotent) so a clean host isn't gated on dirs nothing made.
  # Group-provisioned worker accounts need these directories beneath a QEMU-traversable path.
  # `install -d` is idempotent and avoids inheriting a stale root-only directory from older flows.
  # Skip the sudo elevation when the dir already exists writable by the invoking user: a
  # pre-provisioned CI runner (ansible-created, owned by the runner user) has them, and that
  # service account may lack passwordless sudo (#1293) — only a bare host needs the elevation.
  for _pdir in "$KDIVE_ROOTFS_DIR" "${KDIVE_INSTALL_STAGING:-/var/lib/kdive/install}"; do
    [[ -d "$_pdir" && -w "$_pdir" ]] && continue
    sudo install -d -o "$(id -un)" -m 0755 "$_pdir"
  done
  provision_prereqs_ok || {
    echo "libvirt reachable but provision prerequisites are missing (see MISSING lines)" >&2
    exit 1
  }
else
  banner "libvirt (skipped)"
fi

banner "host processes"
restart_host_processes

banner "inventory reconcile (register images + upload kernel-config siblings to S3)"
# The reconciler daemon reconciles systems.toml on its loop, but run it once synchronously here so a
# completed stack-services.sh GUARANTEES the catalog is fully populated — every declared image registered and
# every on-disk `<name>.config` sibling uploaded with `kernel_config_key` set (ADR-0336) — rather
# than leaving the configs to appear on a later daemon pass. Runs as the invoking user, after the
# daemons start: the synchronous pass and the daemon's own pass are both `reconcile_images`, which
# takes per-row `FOR UPDATE` locks, so concurrent passes serialize safely. Placed after the stack is
# up so a transient reconcile error surfaces (non-zero exit = configs not guaranteed) without tearing
# down a running stack the daemon would otherwise reconcile on its next loop. The CLI resolves the
# inventory path itself (`KDIVE_SYSTEMS_TOML`, else the XDG default) and no-ops on an absent file, so
# no path is recomputed here — a fresh host with no systems.toml is a clean exit-0 pass. Runs with
# only the reconciler's own authority in the environment (#1929 per-daemon pattern).
KDIVE_DATABASE_URL="${KDIVE_RECONCILER_DATABASE_URL}" \
  env -u KDIVE_MIGRATION_DATABASE_URL -u KDIVE_SERVER_DATABASE_URL \
  -u KDIVE_WORKER_DATABASE_URL \
  "$py" -m kdive reconcile-systems || {
  echo "inventory reconcile failed; the catalog may be missing images or kernel configs" >&2
  exit 1
}

banner "status"
"${here}/stack-status.sh"

banner "next: fund a project"
echo "The stack is up but no project is funded yet. Seed budget/quota + mint a token with:"
echo "    just onboard            # project 'demo' (override with KDIVE_PROJECT)"
