#!/usr/bin/env bash
#
# Bring up the WHOLE local kdive infrastructure, idempotently and in order:
#   backends (compose) -> migrations (host) -> libvirt -> host processes -> status.
# Run as the provisioned lifecycle-control operator (never UID 0). Libvirt bring-up is
# unprivileged on a provisioned host (the dedicated session daemon, #2032); only a bare dev host
# still elevates, via sudo, to socket-activate the system daemon.
#
# Usage:
#   scripts/live-stack/up.sh                 full bring-up
#   scripts/live-stack/up.sh --reset-db      wipe the DB first (recovery from migration drift)
#   scripts/live-stack/up.sh --skip-obs      skip prometheus/grafana
#   scripts/live-stack/up.sh --skip-libvirt  backends + host processes only (no VM provisioning)
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
for arg in "$@"; do
  case "$arg" in
  --reset-db) reset_db=1 ;;
  --skip-obs) skip_obs=1 ;;
  --skip-libvirt) skip_libvirt=1 ;;
  *)
    echo "unknown argument: $arg (accepts --reset-db, --skip-obs, --skip-libvirt)" >&2
    exit 2
    ;;
  esac
done
if ((EUID == 0)); then
  echo "up.sh must run as the provisioned lifecycle-control operator, not UID 0" >&2
  exit 1
fi

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
# When KDIVE_OIDC_IMAGE is unset, the oidc service builds from ./deploy/mock-oidc
# (ADR-0357). Pre-build it explicitly so the subsequent `docker compose up` finds
# kdive-mock-oidc:dev locally and skips a doomed pull attempt against that local-only
# tag — which otherwise prints a "pull access denied" warning that looks like a hard
# failure before compose falls back to build anyway. Skip the build when the image
# already exists: the Dockerfile inputs (pom.xml + Dockerfile) change rarely, and
# `docker compose build` re-contacts the registry on every invocation to resolve the
# pinned base-image digests even when every layer is cached. The skip is announced (not
# silent) so an operator editing deploy/mock-oidc knows to remove the tag to force a rebuild.
if [[ -z "${KDIVE_OIDC_IMAGE:-}" ]]; then
  if ! docker image inspect kdive-mock-oidc:dev >/dev/null 2>&1; then
    docker compose build oidc
  else
    echo "using cached kdive-mock-oidc:dev — run 'docker rmi kdive-mock-oidc:dev' to force a rebuild after editing deploy/mock-oidc" >&2
  fi
fi
docker compose up -d "${KDIVE_BACKEND_SERVICES[@]}"
if [[ "$skip_obs" != "1" ]]; then
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
  # needed — do NOT manage virtnetworkd. Gate on `libvirt_ok` (a `virsh list`), not
  # `systemctl is-active`, which reports the *service* inactive on a healthy socket-activated host.
  if ! libvirt_ok; then
    if [[ "$KDIVE_LIBVIRT_URI" == *"live-libvirt"* ]]; then
      # Provisioned-runner recovery (#2032): the dedicated session endpoint is down (fresh boot,
      # reprovision lag). Start the OPERATOR-OWNED session daemon as the invoking user — the same
      # daemon shape the live_vm_host role provisions and keeps boot-persistent via its systemd
      # --user unit. No sudo on this path: the runner service account has none, virtqemud does not
      # exist on the Debian-family runner, and degrading to qemu:///system would hit the
      # root-readback wall (ADR-0223) anyway. If the daemon cannot be started non-interactively,
      # die loud naming the missing paths instead of failing later with a confusing URI error.
      echo "libvirt unreachable at ${KDIVE_LIBVIRT_URI}; starting the dedicated session daemon ..."
      ensure_session_libvirtd || {
        echo "dedicated session daemon could not be started; refusing to fall back to a system daemon" >&2
        exit 1
      }
    else
      # Bare dev host (qemu:///system default): the system daemon is socket-activated, so enable
      # --now plus the re-check below is enough.
      echo "libvirt unreachable; enabling virtqemud.socket (sudo) ..."
      sudo systemctl enable --now virtqemud.socket
    fi
  fi
  libvirt_ok || {
    echo "libvirt daemon not reachable at ${KDIVE_LIBVIRT_URI}" >&2
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
# completed up.sh GUARANTEES the catalog is fully populated — every declared image registered and
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
"${here}/status.sh"

banner "next: fund a project"
echo "The stack is up but no project is funded yet. Seed budget/quota + mint a token with:"
echo "    just onboard            # project 'demo' (override with KDIVE_PROJECT)"
