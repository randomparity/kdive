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
docker compose up -d "${KDIVE_BACKEND_SERVICES[@]}"
if [[ "$skip_obs" != "1" ]]; then
  if ! docker compose --profile obs up -d prometheus grafana; then
    echo "WARNING: observability tier (prometheus/grafana) failed to start; essential stack continues" >&2
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

banner "inventory reconcile (register images + upload kernel-config siblings to S3)"
# The reconciler daemon reconciles systems.toml on its loop, but run it once synchronously here so a
# completed up.sh GUARANTEES the catalog is fully populated — every declared image registered and
# every on-disk `<name>.config` sibling uploaded with `kernel_config_key` set (ADR-0336) — rather
# than leaving the configs to appear on a later daemon pass. Runs as the invoking user (before the
# sudo/libvirt steps) so it reads the user's systems.toml and env. Needs only Postgres + S3, both
# ready now (the backend `up -d` blocks until minio-init created the bucket). A staged-path source
# is declarative, so this never fails on a not-yet-built qcow2; a genuine DB/S3/schema error does
# fail the bring-up, because a green up.sh must mean the configs are actually available. An absent
# systems.toml is the normal pre-config state — skip it.
systems_toml="${KDIVE_SYSTEMS_TOML:-${HOME}/.config/kdive/systems.toml}"
if [[ -f "$systems_toml" ]]; then
  "$py" -m kdive reconcile-systems || {
    echo "inventory reconcile failed; the catalog may be missing images or kernel configs" >&2
    exit 1
  }
else
  echo "no systems.toml at ${systems_toml}; skipping inventory reconcile (no images declared yet)"
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
  # The root worker owns/writes them at provision time; existence is all up.sh requires.
  sudo mkdir -p "$KDIVE_ROOTFS_DIR" "${KDIVE_INSTALL_STAGING:-/var/lib/kdive/install}"
  provision_prereqs_ok || {
    echo "libvirt reachable but provision prerequisites are missing (see MISSING lines)" >&2
    exit 1
  }
else
  banner "libvirt (skipped)"
fi

banner "host processes"
restart_host_processes

banner "status"
"${here}/status.sh"

banner "next: fund a project"
echo "The stack is up but no project is funded yet. Seed budget/quota + mint a token with:"
echo "    just onboard            # project 'demo' (override with KDIVE_PROJECT)"
