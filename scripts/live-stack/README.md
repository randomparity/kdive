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

## Fund a project — `just onboard` (#834)

After the stack is up (`up.sh` or `just stack-up`), `just onboard` funds a project so a fresh
agent's first `allocations.request` is granted instead of hitting the zero-quota/zero-budget wall.
It runs against the **same** `env.sh` DB the rest of the live-stack uses: advisory preflight →
`migrate` → `seed-project` → `verify-project` (the hard funding gate) → mint a 24 h token + print
the **binding contract** (the project string is threaded through the seed, the token claims, and
the contract, so the seeded key, the JWT `projects`/`roles` claim, and the `project` arg match).

```bash
just onboard                 # project "demo"
KDIVE_PROJECT=acme just onboard
```

The minted token expires in 24 h; re-run `just onboard` (or `examples/local-libvirt/mint-token.sh`)
and reconnect your MCP client when it does. `verify-project` echoes the credential-redacted target
DB — if that is not the DB your server reads (a server started with an overriding
`KDIVE_DATABASE_URL` not present here), the project will be funded in the wrong place. Demo-only:
the bundled mock issuer mints a valid token for any caller; production onboards via the audited
admin tools (`docs/operating/project-onboarding.md`).

## Shared

`env.sh` and `lib.sh` are sourced (not run); `apply-migrations.sh` is the host migrator.

> **Note:** `examples/local-libvirt/` has its own `up.sh` / `down.sh` — a guided onboarding
> walkthrough that seeds a project, merges the MCP client config, and tracks processes via a pid
> file. Those are distinct from the host-lifecycle scripts here.
