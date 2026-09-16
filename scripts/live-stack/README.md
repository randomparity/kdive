# Local live-stack scripts

Two entry points, by audience. Pick by whether you need real VM provisioning.

## Full local-libvirt host — `scripts/live-stack/stack-services.sh`

Brings up EVERYTHING needed to provision real VMs, in order: compose backends (+ observability),
DB migrations (this checkout is the authoritative migrator), libvirt, and the host
kdive processes. Server and reconciler run as the configured operator; workers run as isolated
fixed accounts in `kdive-live-worker@1..8.service` through the provisioned lifecycle socket.
Install that host contract first with the `live_vm_host` Ansible role. The supported worker URI is
the explicit operator-owned session socket published in `/etc/kdive/live-worker-libvirt.env`.

Libvirt bring-up follows the configured endpoint: against the published dedicated session
endpoint, `stack-services.sh` recovers a down daemon by starting the operator-owned session daemon as the
invoking user — no sudo (#2032). Only a bare dev host on the `qemu:///system` default still
elevates via sudo to socket-activate the system daemon.

| Command | What it does |
|---------|--------------|
| `stack-services.sh` | full bring-up |
| `stack-services.sh --skip-obs` | skip prometheus/grafana |
| `stack-services.sh --skip-libvirt` | backends + host processes only (no VM provisioning) |
| `stack-services.sh --reset-db` | full `stack-down.sh --wipe` first, then bring up (recovery from migration drift) |
| `stack-down.sh` | stop host processes + backends, **keep** state |
| `stack-down.sh --wipe` | full reset: drop DB/SeaweedFS volumes AND reap `kdive-*` domains + overlays |
| `stack-status.sh` | read-only health of every layer and retained worker slots |

`stack-services.sh --skip-libvirt` skips VM provisioning checks but still requires and uses the installed
systemd worker contract. There is no direct-worker fallback.

`stack-down.sh` and `stack-status.sh` declare themselves libvirt-free with `LIBVIRT_OPTIONAL=1`
(ADR-0660), so a `/etc/kdive/live-worker-libvirt.env` that fails validation leaves them working
with `KDIVE_LIBVIRT_URI` unset instead of aborting them at source time: status reports the endpoint
unresolved, and `--wipe` is refused before any teardown. Every other entry point still fails
closed. Do not export `LIBVIRT_OPTIONAL` in an operator shell — it is a declaration a script makes
about itself, and entry points that need libvirt do not check it.

Run only one live-stack flow per host from `up` through `down`. The lifecycle request lock
serializes individual requests, not whole flows; a later `start` replaces the current fleet.
`worker-lifecycle.sh diagnostics` is bounded to 30 seconds of acquisition, 320 KiB read and
256 KiB emitted per slot, and 1.25 MiB read and 1 MiB emitted per request. If a dependency is
unavailable, restore it and retry the same `status` or `stop`; the retained unit, credential,
state, and database fence are intentional. `stack-down.sh --force` can clear host processes but cannot
publish termination evidence, so it may strand artifact fences.

### Capture publication protocol 4

This checkout supports only a fresh protocol-4 installation. Supply a new empty database and a
new versioned object-store bucket or namespace before `stack-services.sh`; existing protocol-3 data and objects
are not migrated, preserved, inspected, or cleaned. There is no cutover or rollback command.
Worker startup proves conditional-create behavior against the configured store before readiness.

## Backends only — `just stack-backends` (no sudo)

`stack-services.sh --stage backends` under another name: compose backends (Postgres/SeaweedFS/
OIDC) up, the artifacts bucket created and verified, the schema migrated. It starts no host
processes and no libvirt, so it does not by itself serve `just test-live-stack` — that suite needs
the host processes `stack-services.sh` starts. For the container app tier, follow the
[Compose operating guide](../../deploy/compose/README.md), including its worker lifecycle recipes.

## Fund a demo project — `just onboard`

Run `just onboard` after bring-up to seed and verify the selected project's budget/quota and
mint its mock-issuer token. `KDIVE_PROJECT` and `KDIVE_TOKEN_TTL` come from `env.sh`; reconnect
the client after refreshing an expired token. Confirm the helper and server use the same database.
This is demo bootstrap; use [audited project onboarding](../../docs/operating/project-onboarding.md)
for production tenants. The [live-stack runbook](../../docs/operating/runbooks/live-stack.md)
owns the detailed setup and environment procedure.

## Shared

`env.sh` and `lib.sh` are sourced (not run); `apply-migrations.sh` is the host migrator.

The [local-libvirt example](../../examples/local-libvirt/README.md) wraps these scripts with
host preflight, demo funding, and client configuration. It delegates process and worker
lifecycle management here; it has no separate worker or pid-file protocol.
