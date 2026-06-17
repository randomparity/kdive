# Discoverable base-image volume + per-resource staged status

- **Issue:** [#511](https://github.com/randomparity/kdive/issues/511) (feature)
- **ADR:** [`0156`](../adr/0156-discoverable-base-image-volume.md)
- **Companion (merged):** [#513](https://github.com/randomparity/kdive/issues/513) /
  [ADR-0150](../adr/0150-diagnostics-base-image-staging-check.md) — the server-vantage
  `lookup_volume_staged` pool-volume helper this issue reuses.
- **Status:** Draft

## Problem

A user with only MCP access cannot provision on the remote-libvirt host: the **provisionable
base-image volume name is undiscoverable** over MCP, and it lives in a different namespace from the
catalog name every discovery read advertises.

A black-box functional run driving `dhash_entries=1` hit this wall:

- `images_list` and `fixtures_list` each advertise one name, `fedora-kdive-remote-base-43`.
- `systems.provision(base_image_volume="fedora-kdive-remote-base-43")` is **rejected** by the
  worker job: *"base image volume 'fedora-kdive-remote-base-43' is not staged on the remote host's
  storage pool (an operator prerequisite, ADR-0080)"*.
- No MCP read lists what `systems.provision` actually wants (the staged libvirt **volume** name,
  e.g. `fedora-kdive-remote-base-43.qcow2`), nor whether it is staged on the host's pool.

### Ground truth (verified in tree)

- The provider validates `base_image_volume` against the libvirt pool —
  `providers/remote_libvirt/lifecycle/storage.py:126-143` (`ensure_named_overlay` raises the
  "not staged" `CONFIGURATION_ERROR`).
- The catalog name and the staged-volume name are different namespaces; the mapping is
  `ImageEntry.source.volume` (`inventory/model.py` `StagedSource.volume`). It is already persisted:
  the inventory reconcile writes `source.volume` into the `image_catalog.volume` column
  (`inventory/reconcile_images.py:282`, migration `0030_systems_inventory.sql:19`), and
  `ImageCatalogEntry.volume` already exists (`domain/models.py:417`).
- But the reads a user naturally consults drop the column:
  - `images_list` — `mcp/tools/catalog/images.py:37-50` (no `volume` in the row `data`).
  - `fixtures_list` — `mcp/tools/catalog/fixtures.py:35-48` (selects only `provider, name, arch`).
  - `resources_describe` — `mcp/tools/catalog/resources.py:100-118` (pool/host_uri, but no staged
    inventory or staged status).
- `systems.profile_examples` *does* emit the volume (`profile_examples.py:219-235`), but only as a
  worked example for one instance — not on the catalog/resource reads a user browses first.
- ADR-0080 (`docs/adr/0080-…:41-46`) deliberately makes staging an **out-of-band operator
  prerequisite**. This issue does **not** change that — it makes the prerequisite *discoverable and
  verifiable* over MCP. No new MCP path stages a volume.

## Why this is per-resource, not just a global token

Image availability is a property of **a resource**, not of the global catalog. Two hosts can
advertise the same catalog image name yet have different volumes staged on their storage pools. An
agent selecting *which resource to allocate* is influenced by whether that resource can actually
serve the image it needs — so image availability must be answerable **before** `allocations.request`
(which resource it picks depends on it), not discovered only after a provision fails.

This splits the requirement into two distinct discovery signals:

1. **The global token** — the `volume` string `systems.provision` expects. It is the same wherever
   the catalog row is visible, so it belongs on the catalog reads (`images_list` / `fixtures_list`).
2. **Per-resource availability** — *is that volume staged on **this** host's pool, right now?* This
   is read live from the host's vantage and belongs on `resources_describe`, the per-resource read
   that precedes `allocations.request`.

The intended pre-allocation flow becomes:
`images_list` (tokens) → `resources_list` (candidate hosts) → `resources_describe(candidate)` (is the
image staged *here*?) → `allocations.request` on a host that can serve it.

## Acceptance criteria (restated)

- A user with only MCP access can determine, **before** requesting an allocation, the exact
  `base_image_volume` token to pass **and** whether it is staged on a given resource.
- No new MCP path *stages* a volume (staging stays an operator prerequisite, ADR-0080).
- `resources_describe` for a non-remote resource is unchanged.
- A live probe failure **degrades** `resources_describe` to a reported degraded staged status
  (`unreachable` for host/RPC/timeout, `unknown` for unresolvable config) — it never fails the
  describe envelope.

## Design

### 1. Catalog reads carry the `volume` token (read-only, DB-only)

`images_list` and `fixtures_list` already read `image_catalog` rows that carry the `volume` column.
Surface it:

- `images.py` `_row_envelope` adds `"volume": entry.volume or ""` to the row `data`. (Empty string
  for a non-staged row — the envelope `data` is a string map; an S3/build image has no volume.)
- `fixtures.py` `_public_rows` adds `volume` to the `SELECT` and to each emitted row dict.

No DDL, no provider call, no auth change. The RBAC visibility filter on each read is unchanged.

### 2. `resources_describe` reports per-resource staged status (live, best-effort)

When the described resource is `kind == remote-libvirt`, `describe_resource`:

1. Queries `image_catalog` for the caller-visible **staged remote-libvirt** images
   (`provider = 'remote-libvirt' AND volume IS NOT NULL`), applying the same public-plus-viewer
   filter `images_list` uses, ordered `(name, arch)`. This yields `[(name, volume)]`.
2. Calls an **injected probe** `probe(volumes) -> {volume: status}` with just the resolved volume
   names. The production probe resolves the connection config (URI, TLS refs) **and the storage
   pool** from `remote_config_from_inventory()` internally, opens one mutual-TLS `qemu+tls://`
   connection (the same `remote_connection` lifecycle the diagnostics probes and the provisioning
   plane use), and calls the shared `lookup_volume_staged(conn, config.storage_pool, volume)` once
   per volume over that single connection.
3. Merges a structured `staged_base_images` list into the envelope `data`: an ordered list of
   `{ "name": <catalog name>, "volume": <volume token>, "staged": <status> }`.

**The DB connection is released before the probe runs.** The row lookup (step 0, existing) and the
staged-image catalog query (step 1) are fast; they complete and return the pooled Postgres
connection **before** the up-to-5s libvirt probe (step 2) is awaited. The probe holds no DB
connection while it waits on libvirt, so a slow or black-holing host cannot pin a pool slot and
stall unrelated tool calls. (`describe_resource` today wraps the row read in
`async with pool.connection()`; the probe await must sit outside that block.)

**Pool source — `config.storage_pool`, not `resource.pool`.** The probe verifies the pool
provisioning actually uses, which is `config.storage_pool` (`KDIVE_REMOTE_LIBVIRT_STORAGE_POOL`,
read at op time): provisioning creates the overlay there
(`providers/remote_libvirt/lifecycle/provisioning.py:276`) and the #513 base-image-staging
diagnostic probes the same pool (`diagnostics/base_image_staging.py:122`). The `Resource` row's
`pool` **column** is **not** that pool: the inventory reconcile hardcodes it to `'default'` on
create for a config-owned remote resource (`inventory/reconcile_resources.py` — the remote instance
declaration carries no pool), and the row's advertised `storage_pool` capability is explicitly
advisory (`providers/remote_libvirt/discovery.py:94` "the env config stays authoritative for ops").
Probing `resource.pool` would therefore verify the wrong pool — reporting `staged` for a volume
provisioning cannot find (or vice versa) whenever the operator overrides the pool. The probe must
derive the pool from config, which is why step 2 passes only the volume list.

`status` vocabulary (a string per volume):

| status        | meaning                                                                       |
|---------------|-------------------------------------------------------------------------------|
| `staged`      | the volume is present on the host's pool (`VolumeStaging.STAGED`)              |
| `absent`      | the pool exists but the volume is not staged (`VolumeStaging.ABSENT`)          |
| `pool_absent` | the host's configured storage pool does not exist (`VolumeStaging.POOL_ABSENT`)|
| `unreachable` | the host could not be reached / a storage RPC failed / the probe timed out     |
| `unknown`     | the remote config could not be resolved (the probe never opened a connection)  |

The first three map directly from `VolumeStaging` (reusing #513's helper). The last two are the
two distinct **degraded** outcomes, kept separate so the remediation signal stays truthful — the
same `CONFIGURATION_ERROR`-vs-`TRANSPORT_FAILURE` split #513 keeps
(`diagnostics/base_image_staging.py:97-100,124-126`):

- **`unreachable`** — `CategorizedError(TRANSPORT_FAILURE)` (TLS connect failed), any post-open
  `libvirt.libvirtError`, or a timeout. The host or its libvirtd is the problem; the operator's
  action is to check the host. The probe is bounded by a timeout
  (`asyncio.wait_for` around the `asyncio.to_thread` libvirt work, `_STAGED_PROBE_TIMEOUT_SECONDS
  = 5.0` — snappier than the diagnostics sweep's 10s per-check bound, because describe is an
  interactive read) so a black-holing host cannot stall the read. The blocking libvirt work runs in
  a thread, so the event loop is never blocked.
- **`unknown`** — `CategorizedError(CONFIGURATION_ERROR)` from `remote_config_from_inventory()`
  (zero or multiple `[[remote_libvirt]]` instances, malformed inventory, an unsafe URI, or a
  malformed gdbstub range). This probe takes its volume names from the catalog query, **not** from
  `resolve_base_image_staged_volume()`, so the "base image not staged" config-error #513's
  `volume_factory` raises is not a cause here. The inventory/config is the problem, not the host;
  the probe never opened a connection. The operator's action is to fix `systems.toml`, so it must
  not read as "host down".

Either degraded outcome applies to **every** requested volume (one resolution/connection serves the
whole batch). The describe envelope still returns `ok` in all cases — the staged status is advisory
pre-allocation context, never a precondition of describing the resource. If there are **no** staged
remote images visible, `staged_base_images` is an empty list and **no connection is opened** (and no
config is resolved).

The existing `pool` / `cost_class` / `host_uri` keys on `describe_resource` are unchanged.

### Seam / testability

- The probe is a `Callable[[list[str]], Awaitable[dict[str, str]]]` injected into
  `describe_resource` with a production default. Handler unit tests inject a fake that returns a
  canned `{volume: status}` map (and one that raises, to prove the describe still succeeds) — no
  libvirt, no TLS, no network. This mirrors how the diagnostics probes inject `open_connection`.
- The production probe lives in the `remote_libvirt` provider package (it owns the libvirt
  boundary, the config resolution, and the storage-pool selection), reusing `remote_connection` +
  `lookup_volume_staged`. The MCP layer depends on the provider's probe factory, not on libvirt
  directly (the same direction diagnostics → providers already takes).

## Out of scope / explicitly unchanged

- Staging a volume over MCP (stays an operator prerequisite, ADR-0080).
- Probing every resource inside `resources_list` (see ADR considered-and-rejected: it would couple
  a hot list read to the liveness of every configured host and fan out N TLS handshakes per call).
- The `image_catalog` schema (the `volume` column already exists).
- The remote-libvirt singleton constraint (one `[[remote_libvirt]]` instance) is unchanged; the
  per-resource framing is forward-compatible with multiple hosts when that constraint lifts.

## Test plan (behavior, not implementation)

- `images_list`: a staged row carries its `volume`; a non-staged (S3/build) row carries `""`.
- `fixtures_list`: public staged row carries `volume`; ordering deterministic.
- `resources_describe` (remote-libvirt):
  - probe returns mixed statuses → `staged_base_images` reflects each, ordered.
  - no staged remote images visible → empty list, probe not invoked.
  - probe raises (transport) / times out → `unreachable` for all requested volumes, describe `ok`.
  - probe raises config-error → `unknown` for all requested volumes, describe `ok`.
  - the probe is awaited with no DB pool connection held (a probe that blocks does not pin a slot).
  - RBAC: a private staged image owned by another project is not listed.
- `resources_describe` (local-libvirt / fault-inject): no `staged_base_images`, probe never called.
- The production probe (provider-level): probes `config.storage_pool` (not `resource.pool`);
  `STAGED`/`ABSENT`/`POOL_ABSENT` map through; `TRANSPORT_FAILURE`, post-open `libvirtError`, and
  timeout → `unreachable`; `CONFIGURATION_ERROR` → `unknown`; one connection for N volumes.
