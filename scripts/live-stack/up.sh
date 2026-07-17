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
#   scripts/live-stack/up.sh --skip-libvirt  backends + host processes only (no VM provisioning)
#
# No-VM, no-sudo dev loop: KDIVE_WORKER_AS_ROOT=0 scripts/live-stack/up.sh --skip-libvirt
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
# failure before compose falls back to build anyway. `docker compose build` is
# cache-honoring, so repeat runs are near-instant.
if [[ -z "${KDIVE_OIDC_IMAGE:-}" ]]; then
  docker compose build oidc
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

if [[ "$skip_libvirt" != "1" ]]; then
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
  # Own them to the invoking user with mode 0755: the root worker (default) can still write,
  # a KDIVE_WORKER_AS_ROOT=0 worker can now write too, and 0755 keeps the qemu user's traverse
  # bit — needed so the domain can read staged kernels back at boot (ADR-0222/#694). `mkdir -p`
  # left prior runs root:root:0755, tripping the preflight's writable-by-worker check on a
  # non-root worker even though the actual runtime worked. `install -d` is idempotent.
  sudo install -d -o "$(id -un)" -m 0755 "$KDIVE_ROOTFS_DIR" "${KDIVE_INSTALL_STAGING:-/var/lib/kdive/install}"
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
# no path is recomputed here — a fresh host with no systems.toml is a clean exit-0 pass.
"$py" -m kdive reconcile-systems || {
  echo "inventory reconcile failed; the catalog may be missing images or kernel configs" >&2
  exit 1
}

banner "status"
"${here}/status.sh"

banner "next: fund a project"
echo "The stack is up but no project is funded yet. Seed budget/quota + mint a token with:"
echo "    just onboard            # project 'demo' (override with KDIVE_PROJECT)"
