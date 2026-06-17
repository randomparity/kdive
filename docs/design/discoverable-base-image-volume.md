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
- A live probe failure (host down, RPC error, timeout) **degrades** `resources_describe` to a
  reported `unreachable` staged status — it never fails the describe envelope.

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
2. Calls an **injected probe** `probe(pool_name, volumes) -> {volume: status}` with the resource's
   own `pool` and the resolved volume names. The production probe opens one mutual-TLS
   `qemu+tls://` connection (the same `remote_connection` lifecycle the diagnostics probes and the
   provisioning plane use), and calls the shared `lookup_volume_staged(conn, pool, volume)` once per
   volume over that single connection.
3. Merges a structured `staged_base_images` list into the envelope `data`: an ordered list of
   `{ "name": <catalog name>, "volume": <volume token>, "staged": <status> }`.

`status` vocabulary (a string per volume):

| status        | meaning                                                                  |
|---------------|--------------------------------------------------------------------------|
| `staged`      | the volume is present on this host's pool (`VolumeStaging.STAGED`)        |
| `absent`      | the pool exists but the volume is not staged (`VolumeStaging.ABSENT`)     |
| `pool_absent` | the host's configured pool does not exist (`VolumeStaging.POOL_ABSENT`)   |
| `unreachable` | the host could not be reached / the storage RPC failed (degraded)        |

The first three map directly from `VolumeStaging` (reusing #513's helper). `unreachable` is the
degraded outcome: the probe maps `CategorizedError(TRANSPORT_FAILURE)` and any post-open
`libvirt.libvirtError` to `unreachable` for **every** requested volume, and is bounded by a timeout
(`asyncio.wait_for` around the `asyncio.to_thread` libvirt work) so a black-holing host cannot stall
the read. The blocking libvirt work runs in a thread, mirroring the diagnostics probes, so the
event loop is never blocked.

If config resolution fails (no `[[remote_libvirt]]` instance / malformed inventory →
`CategorizedError(CONFIGURATION_ERROR)`), the probe returns `unreachable` for the requested volumes
as well: the describe still succeeds, surfacing the tokens with a "could not verify" status rather
than failing. If there are **no** staged remote images visible, `staged_base_images` is an empty
list and **no connection is opened**.

The existing `pool` / `cost_class` / `host_uri` keys on `describe_resource` are unchanged.

### Seam / testability

- The probe is a `Callable[[str, list[str]], Awaitable[dict[str, str]]]` injected into
  `describe_resource` with a production default. Handler unit tests inject a fake that returns a
  canned `{volume: status}` map (and one that raises, to prove the describe still succeeds) — no
  libvirt, no TLS, no network. This mirrors how the diagnostics probes inject `open_connection`.
- The production probe lives in the `remote_libvirt` provider package (it owns the libvirt
  boundary and the config resolution), reusing `remote_connection` + `lookup_volume_staged`. The
  MCP layer depends on the provider's probe factory, not on libvirt directly (the same direction
  diagnostics → providers already takes).

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
  - probe raises / times out → `unreachable` for all requested volumes, describe still `ok`.
  - RBAC: a private staged image owned by another project is not listed.
- `resources_describe` (local-libvirt / fault-inject): no `staged_base_images`, probe never called.
- The production probe (provider-level): `STAGED`/`ABSENT`/`POOL_ABSENT` map through;
  `TRANSPORT_FAILURE` and post-open `libvirtError` → `unreachable`; one connection for N volumes.
