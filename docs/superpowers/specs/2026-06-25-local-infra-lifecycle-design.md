# Local infrastructure lifecycle scripts

**Date:** 2026-06-25
**Status:** Design — pending review

## Problem

Bringing up the full local kdive stack on this dev host is a hand-rolled, multi-layer ritual
that drifts out of memory between sessions. The layers are:

1. **Compose backends** — Postgres, MinIO (+ bucket init), OIDC. These are containers; the
   reference `docker-compose.yml` also bundles an app tier (`migrate`, `server`, `worker`,
   `reconciler`) built from the stale `kdive:dev` image.
2. **DB migrations** — applied by the *current checkout*, not the bundled image.
3. **libvirt** — `virtqemud` / `virtnetworkd` system daemons (`qemu:///system`), needed for the
   local-libvirt provider. Untouched by any current script.
4. **Host processes** — `server` + `reconciler` (as the invoking user) and `worker` (as root),
   handled today by `scripts/live-stack/restart-stack.sh`.

`restart-stack.sh` covers only layer 4. Everything else is manual, and the manual path has
real traps:

- A bare `docker compose up -d` starts the stale `kdive:dev` app tier, whose bundled `migrate`
  fails the ADR-0015 immutable-migration guard against a DB the current code migrated — and
  whose `server` would contend for port 8000 with the host process.
- libvirt being inactive is silent until a provision attempt fails.
- The compose `migrate`/`server`/`reconciler`/`worker` services must never run on this host.

## Goal

A repeatable, idempotent lifecycle for the **whole** local infrastructure, matching the
ergonomics of `restart-stack.sh` (run via the `!` prefix; self-elevates with `sudo` per layer;
prints build stamps + health at the end).

## Non-goals

- Replacing the compose backends with anything else. They stay long-lived containers.
- Changing the host-process model (server/reconciler as user, worker as root).
- Production / k8s / remote-libvirt deployment. This is local dev only.
- A daemon/supervisor. These are one-shot scripts invoked on demand.

## Design

### New files under `scripts/live-stack/`

#### `lib.sh` (sourced, not executed)

Single source of truth for what is currently duplicated or about to be. Provides:

- `repo_root`, `py` (`.venv/bin/python`), `log_dir`.
- `KDIVE_BACKEND_SERVICES` — the canonical backend compose service list:
  `postgres minio minio-init oidc`. The `obs` profile (`prometheus grafana`) is brought up
  separately so a non-observability run stays lean.
- The daemon process-table matcher, `daemon_pids()`, and `stop_daemons()` — lifted verbatim
  from `restart-stack.sh` (find live `python -m kdive` daemons by `ps`, sudo-kill a root worker).
- `report_build_stamps()` — the "expect g<HEAD>" reporter from `restart-stack.sh`.
- `server_health()` — curl `/mcp`, expect 401 (= up, auth required).
- `libvirt_ok()` — `virsh -c "${KDIVE_LIBVIRT_URI:-qemu:///system}" list` returns 0.

`restart-stack.sh` is refactored to `source lib.sh` and drop its now-shared copies. This is
behavior-preserving: the lifted functions keep their current logic.

#### `up.sh` — full bring-up

Idempotent, ordered, fails fast (`set -euo pipefail`). Run from anywhere via
`! scripts/live-stack/up.sh`.

1. **Preflight** — `.venv` python exists (else "run just setup"); `docker` reachable; resolve
   repo root.
2. **Backends** — `docker compose up -d ${KDIVE_BACKEND_SERVICES}`, then, unless
   `KDIVE_SKIP_OBS=1`, `docker compose --profile obs up -d prometheus grafana`. Never the
   `kdive:dev` app tier. Wait for `postgres` to report healthy
   (`docker compose ps --format` poll, bounded ~30 s).
3. **Migrations** — `bash scripts/live-stack/apply-migrations.sh` (current checkout =
   authoritative migrator). The compose `migrate` service stays off.
4. **libvirt** — if `virtqemud`/`virtnetworkd` are inactive,
   `sudo systemctl enable --now virtqemud.socket virtnetworkd.socket`
   (socket-activated modular daemons; `enable` persists across reboot per the chosen policy).
   Then assert `libvirt_ok`; fail with a clear message if not.
5. **Host processes** — exec/delegate to `scripts/live-stack/restart-stack.sh` (server +
   reconciler as user, worker as root). All `KDIVE_*` knobs flow through unchanged.
6. **Report** — invoke `status.sh`.

#### `down.sh` — teardown

- `stop_daemons` (from `lib.sh`; sudo for the root worker).
- `docker compose --profile obs down` (backends + observability). **Keeps named volumes by
  default** so the Postgres DB and MinIO artifacts survive. A `--wipe` flag adds `-v` for a
  clean-slate DB (prints a warning first).
- libvirt is **left enabled and running** — it is a host service; cycling it on every teardown
  is hostile and slow. (Documented in the script header.)

#### `status.sh` — read-only health

No side effects. Reports, per layer:

- **Backends** — `docker compose ps` for the backend + obs services.
- **Host daemons** — process table + `report_build_stamps` (expect `g<HEAD>`).
- **App health** — `server_health` (`/mcp` → 401).
- **DB** — quick `psycopg`/`pg_isready`-style reachability on `KDIVE_DATABASE_URL`.
- **libvirt** — `libvirt_ok` against `qemu:///system`.

### Grafana in compose (chosen: add it now)

`grafana` is not currently a compose service (the `kdive-grafana` container seen on this host
was an ad-hoc `docker run`, not reproducible). Add a `grafana` service to `docker-compose.yml`
under the existing `obs` profile so `up.sh` brings up Prometheus **and** Grafana reproducibly:

- `image: grafana/grafana:<pinned>` (pin a version, not `latest`).
- `profiles: ["obs"]`, `depends_on: [prometheus]`, publish `3000:3000`.
- Anonymous admin / sign-up disabled appropriate for a local demo (env: anonymous viewer or
  `GF_AUTH_ANONYMOUS_ENABLED=true`, `GF_SECURITY_ADMIN_PASSWORD` for the demo — local only).
- **Provisioning mounts** (read-only), new files under `deploy/compose/grafana/`:
  - `provisioning/datasources/prometheus.yml` — a default Prometheus datasource at
    `http://prometheus:9090` (compose-network name), matching the `${datasource}` variable the
    dashboard expects.
  - `provisioning/dashboards/kdive.yml` — a file provider pointing at a mounted dashboards dir.
  - mount the existing `deploy/grafana/kdive-overview.json` into that dir read-only so the
    overview dashboard auto-loads.

Prometheus's TSDB stays ephemeral (matches the current demo posture). Grafana state can be
ephemeral too (provisioning is declarative on every start).

## Idempotency & failure behavior

- Every layer is safe to re-run: `compose up -d` is convergent; `apply-migrations.sh` only
  applies new migrations; `systemctl enable --now` is a no-op when already active;
  `restart-stack.sh` stops-then-starts.
- `set -euo pipefail` throughout. Each layer prints a clear banner; a failed layer aborts with
  a message naming the layer and the remediation.

## Testing

These are host-orchestration scripts that drive `sudo`, `docker`, and `systemctl`, so unit
testing has low value. Verification is:

- `shellcheck` + `shfmt -d` clean on all four scripts (matches repo bash standard).
- `docker compose config` validates the amended `docker-compose.yml`.
- **Live smoke on this host** (the canonical proof): from a torn-down state, one `up.sh` run
  reaches a healthy stack — `status.sh` shows backends up, host daemons on `g<HEAD>`, server
  401, libvirt reachable, Grafana serving the kdive-overview dashboard at `:3000`. Then a
  local-libvirt provision succeeds (the original task that motivated this).
- `down.sh` (no flag) leaves the Postgres volume intact (re-`up.sh` keeps prior data);
  `down.sh --wipe` produces a clean DB.

## Open items folded into the plan

- Pin the Grafana image version (look up current stable, not from memory).
- Confirm `minio-init` is included as an explicit `up` target (it is a one-shot init job).
- Decide grafana auth posture for local (anonymous viewer vs. admin password) — local-only,
  documented in `deploy/compose/README.md`.
