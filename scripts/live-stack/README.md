# Local live-stack scripts

Two entry points, by audience. Pick by whether you need real VM provisioning.

## Full local-libvirt host — `scripts/live-stack/up.sh` (needs sudo)

Brings up EVERYTHING needed to provision real VMs, in order: compose backends (+ observability),
DB migrations (this checkout is the authoritative migrator), libvirt (`virtqemud`), and the host
kdive processes (server + reconciler as you, worker as root). Self-elevates with `sudo`; run via
the `!` prefix in the agent or directly in a shell.

| Command | What it does |
|---------|--------------|
| `up.sh` | full bring-up |
| `up.sh --skip-obs` | skip prometheus/grafana |
| `up.sh --skip-libvirt` | backends + host processes only (no VM provisioning) |
| `up.sh --reset-db` | full `down.sh --wipe` first, then bring up (recovery from migration drift) |
| `down.sh` | stop host processes + backends, **keep** state |
| `down.sh --wipe` | full reset: drop DB/MinIO volumes AND reap `kdive-*` domains + overlays |
| `status.sh` | read-only health of every layer |

No-VM, no-sudo dev loop (just poke the MCP API against backends):
`KDIVE_WORKER_AS_ROOT=0 scripts/live-stack/up.sh --skip-libvirt`.

## Backends only — `just stack-up` (no sudo)

Brings up only the compose backends (Postgres/MinIO/OIDC) and migrates the schema — for the
`just test-live-stack` suite, or to run the app tier from the compose reference
(`docker compose up -d migrate server worker reconciler`). Does NOT start host processes or libvirt.

## Shared

`env.sh` and `lib.sh` are sourced (not run); `apply-migrations.sh` is the host migrator.

> **Note:** `examples/local-libvirt/` has its own `up.sh` / `down.sh` — a guided onboarding
> walkthrough that seeds a project, merges the MCP client config, and tracks processes via a pid
> file. Those are distinct from the host-lifecycle scripts here.
