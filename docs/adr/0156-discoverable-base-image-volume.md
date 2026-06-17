# ADR 0156 — Discoverable base-image volume + per-resource staged status over MCP

- **Status:** Accepted
- **Date:** 2026-06-17
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0080](0080-remote-provisioning-disk-image-profile.md)
  (the operator-staged base volume is an out-of-band prerequisite — unchanged here),
  [ADR-0150](0150-diagnostics-base-image-staging-check.md) (the server-vantage
  `lookup_volume_staged` pool-volume helper this reuses), [ADR-0092](0092-image-rootfs-lifecycle.md) /
  [ADR-0112](0112-systems-inventory-config.md) (the `image_catalog` row and the `systems.toml`
  `StagedSource.volume` mapping persisted into it), [ADR-0019](0019-tool-response-envelope.md)
  (the response envelope), [ADR-0047](0047-agent-facing-tool-guide-generation.md) (the generated reference).
- **Spec:** [`../design/discoverable-base-image-volume.md`](../design/discoverable-base-image-volume.md)

## Context

A user with only MCP access cannot provision on the remote-libvirt host. `systems.provision` wants
the operator-staged libvirt **volume** name (e.g. `fedora-kdive-remote-base-43.qcow2`), but every
discovery read advertises the catalog **image** name (`fedora-kdive-remote-base-43`) — a different
namespace — and no read says whether the volume is staged on the host's pool. ADR-0080 makes
staging an out-of-band operator prerequisite; the gap is that the prerequisite is neither
discoverable nor verifiable over MCP, so a black-box agent reaches a hard wall at provision time.

Two facts shape the decision:

1. The catalog→volume mapping **already exists** in the `image_catalog.volume` column (written from
   `StagedSource.volume` by the inventory reconcile, migration 0030). Surfacing the token is a pure
   read-projection change — no DDL, no provider call.
2. Whether a volume is *staged* is a property of **a specific host's storage pool**, not of the
   global catalog. Two hosts can carry the same catalog name with different volumes staged. An
   agent's choice of which resource to allocate depends on which resource can serve the image it
   needs, so per-resource availability must be answerable **before** `allocations.request`.

## Decision

### 1. The `volume` token is surfaced on the catalog reads

`images_list` and `fixtures_list` add the `image_catalog.volume` value to each row they emit
(empty string for a non-staged S3/build row). This is the exact token `systems.provision` expects,
carried by the entry a user already browses. DB-only, RBAC filters unchanged.

### 2. `resources_describe` reports per-resource staged status from the server vantage

For a `remote-libvirt` resource, `describe_resource` queries the caller-visible staged
remote-libvirt catalog images, then calls an **injected probe** that opens one `qemu+tls://`
connection (the shared `remote_connection` lifecycle) and runs the shared `lookup_volume_staged`
helper (ADR-0150) once per volume over that single connection. The result is merged into the
envelope `data` as an ordered `staged_base_images` list of `{name, volume, staged}`, where
`staged` is one of `staged` / `absent` / `pool_absent` / `unreachable` / `unknown`.

The probe verifies **`config.storage_pool`** — the pool provisioning actually creates the overlay
in (`provisioning.py:276`) and the pool #513's diagnostic probes — **not** the `Resource` row's
`pool` column. That column is hardcoded to `'default'` on create for a config-owned remote resource
(`inventory/reconcile_resources.py`) and the advertised `storage_pool` capability is advisory
(`discovery.py:94`); probing it would verify the wrong pool whenever the operator overrides
`KDIVE_REMOTE_LIBVIRT_STORAGE_POOL`. The probe resolves the pool from config internally, so the
handler passes only the volume list.

The probe is **best-effort and bounded**: a TLS-connect failure, a post-open `libvirtError`, or a
timeout degrade to `unreachable`; an unresolvable config degrades to `unknown` (kept distinct so
"fix systems.toml" never reads as "host down", matching #513's CONFIGURATION_ERROR-vs-TRANSPORT
split). Either way the describe envelope still returns `ok`. The blocking libvirt work runs in a
thread under a bounded `asyncio.wait_for` (5s — interactive-read snappy, vs the diagnostics sweep's
10s), so the event loop is never blocked and a black-holing host cannot stall the read. When no
staged remote image is visible, no connection is opened.

### 3. The probe is owned by the provider package, injected into the MCP handler

The production probe lives in `providers/remote_libvirt/` (it owns the libvirt boundary and config
resolution) and is injected into `describe_resource` with a default. Handler unit tests inject a
fake — no libvirt, no network — mirroring how the diagnostics probes inject their opener. The MCP
layer depends on the provider's probe factory, not on libvirt directly (the established
diagnostics → providers import direction).

## Consequences

- A black-box agent can read the exact `base_image_volume` token (catalog reads) and verify it is
  staged on a chosen resource (`resources_describe`) before allocating — closing the provision wall
  without changing the ADR-0080 prerequisite.
- `resources_describe` for a remote-libvirt resource now performs a live, bounded TLS read. It is
  best-effort: a degraded probe surfaces `unreachable`/`unknown` rather than failing the read, so a
  host outage or a config drift never blinds the rest of the description. Describe latency for a remote resource gains one
  bounded round-trip; non-remote describes are untouched.
- `resources_list` stays DB-only (no live probe), so the high-frequency list read keeps its current
  cost and failure profile; staged status is fetched on demand per candidate via describe.
- No new MCP path stages a volume; staging remains an operator prerequisite.

## Considered & rejected

- **Probe staged status inside `resources_list`.** A one-shot "availability across all candidates"
  view, but it couples a hot, high-frequency list read to the liveness of *every* configured host
  and fans out N TLS handshakes per call. Describe-level probing is on-demand, bounded to the one
  host the agent is considering, and still precedes allocation. Rejected; revisit only if a
  one-shot matrix is shown to be needed.
- **Add a `volume` query parameter / a new `images.staged` tool.** A new tool surface for what is
  one extra field on existing reads plus one section on describe. Enriching the reads an agent
  already consults is the smaller, more discoverable change. Rejected.
- **Resolve staged status from the DB / a cached value instead of a live pool read.** The DB knows
  the catalog→volume mapping but not whether the operator has staged the volume on the host's pool
  (that is exactly the out-of-band ADR-0080 step the DB never observes). Only a server-vantage pool
  read answers "is it staged?"; a cached value would lie after an operator stages/unstages.
  Rejected.
- **Fail `resources_describe` when the probe cannot reach the host.** A host outage would then
  blind the entire resource description (pool, cost, caps), and the staged status is advisory
  pre-allocation context, not a hard precondition of describing a resource. Degrade to
  `unreachable` instead. Rejected.
- **Stage the volume over MCP (an `images.stage` mutation).** Reverses ADR-0080's deliberate
  operator-prerequisite boundary and would need host-side image transfer the remote provider does
  not own. Out of scope. Rejected.
