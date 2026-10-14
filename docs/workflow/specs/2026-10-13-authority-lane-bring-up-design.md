# Installed authority lane bring-up without manual overrides (#3215)

## Problem

The #3184 live proof on the installed local-authority lane needed three manual overrides after a
normal `scripts/live-stack/stack-services.sh` bring-up:

1. **No worker authority route.** `scripts/live-stack/worker-lifecycle.sh` copies the five
   `KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_{INSTANCE,REQUEST_SOCKET,SERVER_CA_REF,CLIENT_CERT_REF,
   CLIENT_KEY_REF}` values into the start request only from its own environment. Only the
   `live_vm_host` role knows them, and it publishes them nowhere a bring-up can read. The frame's
   route gate then fails with "installed worker authority route is incomplete".
2. **Malformed default DSNs.** `scripts/live-stack/env.sh` builds
   `localhost:${KDIVE_POSTGRES_PORT}/kdive`. The lane publishes Postgres with an `ADDR:PORT`
   value, so every default DSN, the harness DSN included, becomes `localhost:ADDR:PORT`, which
   psycopg rejects. The SeaweedFS and OIDC URL defaults in the same file interpolate their port
   variables the same way.
3. **No fixture-mode resource.** The lane's only `[[local_libvirt]]` entry carries
   `authority_instance`, so the frame's fixture mode stops `blocked` after allocating.
   A plain entry *beside* it is not expressible. Local-libvirt resource rows are one per
   discovered libvirt endpoint: `_overlay_one_local` in
   `src/kdive/inventory/reconcile/resources.py` binds each entry by `host_uri`, so a second entry
   renames the same row. The issue's alternative applies: document the supported configuration.

## Decision

[ADR-0770](../../adr/0770-installed-worker-authority-route-is-published.md) records the route
contract: the role publishes it, the lifecycle client forwards it, and a contradicting preset is
refused.

## Design

### Route publication (`live_vm_host` role)

With `live_vm_host_worker_authority_enabled`, the role writes `/etc/kdive/live-worker-authority.env`
as root:root `0644`. The file holds exactly five lines, `NAME=value`, in the order above. The
values come from `live_vm_host_worker_authority_{instance,request_socket,server_ca_ref,
client_certificate_ref,client_key_ref}`. With the client disabled, the role removes the file.
`verify.yml` checks the owner, mode and exact content when enabled, and absence when disabled.
`playbooks/authority_host_teardown.yml` removes the file with the other authority endpoint
artifacts and asserts that it is gone. The file carries no credential bytes. The settings
registry marks `KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_CLIENT_KEY_REF` `secret=True`
(`src/kdive/providers/external_boot_authority/settings.py`) so that it is redacted, but its value
is a root-relative path to a `0440` file under a `0750` root. Publishing it at `0644` discloses a
path that is already the role default; the key stays unreadable to non-members.

### Route forwarding (`worker-lifecycle.sh start`)

`load_published_authority_route` runs after `load_published_libvirt_uri` in the `start` branch.
`AUTHORITY_ROUTE_ENV` is an unprefixed test seam with the default above.

- File absent (and not a dangling symlink): return 0 and change nothing. A host without a worker
  authority client keeps today's behaviour.
- Otherwise `require_exact_file … 644` must pass. The file must hold exactly five lines, each
  `NAME=value`, with every name from the five-name allowlist, no name twice, and a non-empty
  value. The content is read with `mapfile` as literal data and is never evaluated.
- A name already set in the environment to a different value fails with a message that names the
  variable and the file. An equal value, or an unset name, is exported from the file.

The lifecycle request schema is unchanged. Stack restarts run `start` again, so the route is
re-read on every bring-up.

### Backend endpoints (`env.sh`)

`backend_endpoint NAME` turns a port variable's value into the `host:port` used by the client URL
defaults:

- `PORT`, or `ADDR:PORT` whose address is empty, `127.0.0.1`, `0.0.0.0`, `::1`, `::`, `[::1]` or
  `[::]`, gives `localhost:PORT`. That keeps today's default URLs, including the OIDC issuer
  identity, byte for byte.
- Any other address gives `ADDR:PORT`. An unbracketed IPv6 address is bracketed.
- Otherwise it fails: the port must be 1–65535 digits and the address must match
  `[][A-Za-z0-9.:-]+`. The message names the variable, the value and the accepted forms, and
  `env.sh` returns non-zero to its caller.

The Postgres, SeaweedFS and OIDC defaults use it. Explicit URL variables still win. The
descriptions in `src/kdive/config/external_env.py` and the generated config reference say that
`ADDR:PORT` is accepted.

### Fixture-mode inventory (frame and runbook)

`select_lane` in `tests/integration/live_stack/authority_lane.py` loads `KDIVE_SYSTEMS_TOML` with
`kdive.inventory.loader.load_inventory`. If it declares local-libvirt entries and every one of them
carries `authority_instance`, the frame stops `blocked` before any read or write, naming the
supported configuration. A file that fails to load (`InventoryError`) also stops `blocked`,
naming the variable. The existing check on the allocated resource stays. The runbook documents
that configuration: a separate copy of the lane inventory with only the `[[local_libvirt]]`
entry's `authority_instance` line removed. The routed inventory file and the
`[[remote_libvirt]]` authority tuple are unchanged; the local resource is unrouted only while the
stack runs on the copy. The server and fixed workers read the inventory themselves, so the whole
stack is brought up with `KDIVE_SYSTEMS_TOML` naming the fixture copy and the harness exports the
same path. Switching back to the routed file is another `stack-services.sh` run.

### Runbooks

`docs/operating/runbooks/live-testing.md` gains three things. Candidate evidence says the
bring-up forwards the installed route itself and that a contradicting export is refused. The
frame section documents the fixture-mode inventory and the `ADDR:PORT` port handling, and
drops the need for a hand-written harness DSN. The self-hosted runner runbook names the
published file. `scripts/live-stack/README.md` gets one sentence on the route file.

## Failure model

1. **Actors and deployments:** a local operator running the live-stack scripts on an installed
   authority lane or a developer host; the `live_vm_host` play run by an operator.
2. **Invariants and assets at stake:** a worker must not get a route other than the installed
   authority's. Default DSNs must reach the published backend. Today's default URLs must not
   change for bare `PORT` values. The routed inventory file and the remote authority tuple stay
   unchanged; the local resource is unrouted only while the stack runs on the fixture copy.
3. **Accepted failure classes:**
   - A caller who can set `AUTHORITY_ROUTE_ENV` and shadow `stat` on PATH can feed its own route.
     That caller already owns the environment the five values come from today.
   - A host that is not loopback but resolves to a loopback address is used as written.
   - A hand-edited route file is caught by the allowlist or by the lifecycle schema.
4. **Covered elsewhere:** route values are validated by `LifecycleRequest.validate_authority_route`
   and the worker gate. Public authority provisioning is #3144/#3148. HTTP cell qualification is
   #3186. Remote framing is #3209. The compose-recipe witness DSN hazard is
   `deploy/compose/README.md` and is not changed here.

### Threat model

- **Boundaries:** one new boundary, a root-owned `/etc/kdive` file read by an operator script.
  Port variables already crossed into URLs; this change widens the parsing.
- **Actors:** a local operator account (trusted for its own environment); a non-root local user
  who could try to plant a file (stopped by root:root `0644` metadata, no symlink, and `/etc/kdive`
  being root:root `0755`).
- **Controls:** metadata check, a five-name allowlist, `mapfile` literal parsing with no `eval`,
  the indirect expansion `${!name}` only after the allowlist match, refusal of contradictions,
  and an address/port charset check before interpolation.
- **Out of scope:** credential bytes. The file carries refs only, and the key itself stays in
  the protected secrets root.

## Testing

- `tests/scripts/test_live_stack_scripts.py`: route loader cases (absent, valid, bad metadata,
  extra/duplicate/unknown/empty lines, contradicting preset, equal preset, `$(…)` value stays
  literal). The start request carries the five values from the file. Endpoint cases (bare port,
  loopback, wildcard, LAN address, bracketed and bare IPv6, invalid port, invalid address).
- `tests/deploy/test_live_worker_provisioning.py`: the role publishes, verifies and withdraws the
  file. Teardown removes it.
- `tests/integration/live_stack/test_authority_lane.py`: an all-routed inventory and an invalid
  inventory stop `blocked` with no calls; a plain or empty-local inventory proceeds.
- Live: the #3184 frame proof on the installed lane with none of the three overrides, under a
  separately approved plan.
