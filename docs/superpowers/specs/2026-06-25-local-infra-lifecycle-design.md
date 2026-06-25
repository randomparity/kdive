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
- `libvirt_ok()` — `virsh -c "${KDIVE_LIBVIRT_URI:-qemu:///system}" list` returns 0
  (daemon reachable; sufficient given user-mode networking + file-based storage).
- `provision_prereqs_ok()` — `qemu-img` on PATH and `/var/lib/kdive/rootfs`
  (+ `KDIVE_INSTALL_STAGING`) exist and are writable by the root worker.
- `kdive_domains()` — list `kdive-*` libvirt domains (used by `down.sh --wipe` reaping).

`restart-stack.sh` is refactored to `source lib.sh` and drop its now-shared copies. This is
behavior-preserving: the lifted functions keep their current logic.

#### `up.sh` — full bring-up

Idempotent, ordered, fails fast (`set -euo pipefail`). Run from anywhere via
`! scripts/live-stack/up.sh`.

1. **Preflight** — `.venv` python exists (else "run just setup"); `docker` reachable; resolve
   repo root.
2. **Reconcile app tier** — `docker compose rm -sf migrate server worker reconciler` first.
   A subset `up -d` of the backends does **not** create the app tier (verified: nothing
   `depends_on` them), but a previously running compose `server` would hold port 8000 against
   the host process. This defensively removes any such container before the host tier starts,
   closing the contention risk named in the Problem section.
3. **Backends** — `docker compose up -d ${KDIVE_BACKEND_SERVICES}`, then, unless
   `KDIVE_SKIP_OBS=1`, `docker compose --profile obs up -d prometheus grafana`. Never the
   `kdive:dev` app tier. Wait for `postgres` to report healthy
   (`docker compose ps --format` poll, bounded ~30 s).
4. **Migrations** — `bash scripts/live-stack/apply-migrations.sh` (current checkout =
   authoritative migrator). The compose `migrate` service stays off. **On non-zero exit**
   (e.g. the ADR-0015 `applied migration … checksum changed` guard — see "Migration drift"
   below) `up.sh` aborts with the explicit remediation: re-run as `up.sh --reset-db`, which
   wipes the Postgres volume and re-migrates from the current checkout.
5. **libvirt** — if `virtqemud`/`virtnetworkd` are inactive,
   `sudo systemctl enable --now virtqemud.socket virtnetworkd.socket`
   (socket-activated modular daemons; `enable` persists across reboot per the chosen policy).
   Then assert `libvirt_ok`. The provider needs **no** libvirt-managed network or storage pool
   (see "What the provider actually needs"), so a reachable daemon is a sufficient libvirt
   signal — but `up.sh` also asserts the two host prerequisites a provision *does* need:
   `qemu-img` on PATH and a writable `/var/lib/kdive/rootfs` (+ `KDIVE_INSTALL_STAGING`),
   owned by the root worker. Fail with a clear message naming whichever is missing.
6. **Host processes** — exec/delegate to `scripts/live-stack/restart-stack.sh` (server +
   reconciler as user, worker as root). All `KDIVE_*` knobs flow through unchanged. This
   inherits `restart-stack.sh`'s root-worker requirements (`KDIVE_KERNEL_SRC`,
   `KDIVE_BUILD_USER`); `up.sh` does not re-implement them.
7. **Report** — invoke `status.sh`.

#### `down.sh` — teardown

- `stop_daemons` (from `lib.sh`; sudo for the root worker).
- `docker compose --profile obs down` (backends + observability). **Keeps named volumes by
  default** so the Postgres DB and MinIO artifacts survive. A `--wipe` flag adds `-v` for a
  clean-slate DB (prints a warning + confirmation first).
- **`--wipe` also reaps libvirt-side state**, because kdive-provisioned domains and their
  qcow2 overlays live *outside* compose and outside the Postgres volume. Wiping only the DB
  would orphan running `kdive-*` domains (the DB no longer references them, so the reconciler
  cannot reap them) and leave their `/var/lib/kdive/rootfs/<id>-overlay.qcow2` files behind.
  So `--wipe` enumerates `kdive_domains()`, `virsh destroy`+`undefine`s each (sudo), and removes
  the matching overlays. Plain `down.sh` (no flag) leaves both the DB and any running domains
  intact — it is a "stop the stack," not a "reset."
- libvirt itself is **left enabled and running** — it is a host service; cycling the daemon on
  every teardown is hostile and slow. (Documented in the script header.)

#### `status.sh` — read-only health

No side effects. Reports, per layer:

- **Backends** — `docker compose ps` for the backend + obs services.
- **Host daemons** — process table + `report_build_stamps` (expect `g<HEAD>`).
- **App health** — `server_health` (`/mcp` → 401).
- **DB** — quick `psycopg`/`pg_isready`-style reachability on `KDIVE_DATABASE_URL`.
- **libvirt** — `libvirt_ok` (daemon) **and** `provision_prereqs_ok` (`qemu-img` +
  `/var/lib/kdive/rootfs`), reported as distinct lines so a reachable-but-not-provision-ready
  host is not mistaken for green.

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

## What the provider actually needs (libvirt prerequisites)

Pinned against the code so the readiness checks are honest, not aspirational:

- **Networking:** QEMU **user-mode SLIRP** (`-netdev user`, `lifecycle/xml.py`). There is **no**
  libvirt-managed network and the `default` network is **not** required. So `up.sh` must not try
  to start/validate a libvirt network.
- **Storage:** per-System qcow2 overlays created with `qemu-img` into `ROOTFS_DIR`
  (`/var/lib/kdive/rootfs`, `lifecycle/storage.py`). There is **no** libvirt storage pool;
  `_POOL = "local-libvirt"` in `composition.py` is the kdive *scheduling-pool label* (#561), a
  different concept. So `up.sh` must not try to define a libvirt storage pool.
- **Therefore** libvirt readiness = daemon reachable (`virsh list`) **plus** host filesystem
  prerequisites (`qemu-img`, writable `/var/lib/kdive/rootfs`, `KDIVE_INSTALL_STAGING`) — these
  are what actually make the "silent until provision fails" gap real, and what the checks cover.

## Migration drift (persistent volume vs. checked-out branch)

The ADR-0015 immutable-migration guard fires whenever a migration recorded as *applied* in the
DB has different file content than the current checkout — it is image-agnostic, so it bites the
**host** `apply-migrations.sh` too, not just the stale compose `migrate`. Because `down.sh`
keeps the Postgres volume by default, routine dev (switching branches, editing an unmerged
migration, rebasing) can leave the persisted history diverged from the working tree, and a plain
`up.sh` will then abort at the migrations step. This is expected and handled: `up.sh` surfaces
the failure with the `--reset-db` remediation rather than failing opaquely. `--reset-db` is
equivalent to `down.sh --wipe` followed by `up.sh`.

## Idempotency & failure behavior

- Every layer is safe to re-run: `compose up -d` is convergent; `systemctl enable --now` is a
  no-op when already active; `restart-stack.sh` stops-then-starts.
- **Migrations are convergent only when the history matches** — `apply-migrations.sh` applies
  new migrations *or aborts* on the immutable-migration guard (see "Migration drift"). `up.sh`
  treats that abort as a recoverable, remediated state (`--reset-db`), not a silent retry loop.
- `set -euo pipefail` throughout. Each layer prints a clear banner; a failed layer aborts with
  a message naming the layer and the remediation.

## Testing

These are host-orchestration scripts that drive `sudo`, `docker`, and `systemctl`, so unit
testing has low value. Verification is:

- `shellcheck` + `shfmt -d` clean on all four scripts (matches repo bash standard).
- `docker compose config` validates the amended `docker-compose.yml`.
- **Invariant guard** (cheap, automated): a grep/test asserting `up.sh` never names the app-tier
  services as `compose up` targets and never starts the compose `migrate`. These are the two
  invariants most likely to silently regress; the repo's "verify at every level" standard wants
  them machine-checked rather than left to live smoke.
- **Live smoke on this host** (the canonical proof): from a torn-down state, one `up.sh` run
  reaches a healthy stack — `status.sh` shows backends up, host daemons on `g<HEAD>`, server
  401, libvirt reachable + provision-prereqs green, and **Grafana renders the kdive-overview
  dashboard with live Prometheus data** at `:3000` (not merely "serves a page" — the
  `${datasource}` variable must resolve to the provisioned default datasource). Then a
  local-libvirt provision succeeds (the original task that motivated this).
- `down.sh` (no flag) leaves the Postgres volume *and* any running `kdive-*` domains intact
  (re-`up.sh` keeps prior data); `down.sh --wipe` produces a clean DB **and** leaves no orphaned
  `kdive-*` domains or overlay files.
- **Migration-drift path:** with a deliberately diverged applied-migration history, `up.sh`
  aborts at migrations with the `--reset-db` remediation, and `up.sh --reset-db` recovers to a
  healthy stack.

## Open items folded into the plan

- Pin the Grafana image version (look up current stable, not from memory).
- Confirm `minio-init` is included as an explicit `up` target (it is a one-shot init job).
- Decide grafana auth posture for local (anonymous viewer vs. admin password) — local-only,
  documented in `deploy/compose/README.md`.
- Ensure the provisioned Prometheus datasource is `isDefault: true` so the dashboard's
  `${datasource}` template variable resolves without manual selection.
