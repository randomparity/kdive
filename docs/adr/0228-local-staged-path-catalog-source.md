# ADR 0228 — Local-libvirt staged-path image source for catalog rootfs discovery

- **Status:** Accepted
- **Date:** 2026-06-23
- **Deciders:** kdive maintainers

## Context

Local-libvirt's quick-start provisions a rootfs with a `local` reference
(`{kind:"local", path:"/var/lib/kdive/rootfs/…"}`, `components/references.py:57`) that points at
a raw file under the provider's `allowed_roots` (`components/local_paths.py`). That file is never
registered in `image_catalog`, so no MCP discovery surface enumerates it: `fixtures.list` /
`images.list` query the catalog table, and `systems.profile_examples` projects declared
`[[image]]` entries. A pure-MCP agent (no host shell) is therefore forced to `ls` the host to
find a provision-able rootfs — defect **D1** of the black-box review (#732, part of #736; same
root cause as the closed #449 / #618: the discovery surface holds nothing the caller can act on).

Remote-libvirt has no such gap. Operators declare `[[image]]` entries whose source is `s3`
(object), `build` (built in-tree), or `staged` (an operator-staged provider **volume**, no S3
object — `inventory/model.py:54`). Each seeds an `image_catalog` row (`reconcile_images.py`) that
`fixtures.list`/`images.list` surface and the `catalog` rootfs lane resolves. The `staged` source
already proves the catalog spans non-S3, host-resident images: a registered row may carry a
`volume` instead of an `object_key` (migration `0030`, `image_object_present` CHECK).

The catalog is the single provider-neutral, RBAC-aware discovery surface (ADR-0092/0093). The
asymmetry is purely that local-libvirt has no storage-pool **volume** abstraction — its
host-resident rootfs is an absolute filesystem path under `allowed_roots`, which the existing
`staged`/`volume` source cannot express.

## Decision

Bring local-libvirt's operator-staged rootfs into the catalog as a first-class image, so the
existing discovery surface covers it and the `catalog` lane resolves it.

1. **Inventory source.** Add `StagedPathSource` (`kind:"staged-path"`, `path: str` — absolute) to
   the `ImageSource` union (`inventory/model.py`), alongside `s3`/`build`/`staged`. It declares an
   operator-staged rootfs file resolvable on a configured local-libvirt provider root.

2. **Persistence.** Add a nullable `image_catalog.path` column (migration `0047`). A non-`defined`
   row carries **exactly one** of `{object_key, volume, path}`; the `image_object_present` CHECK
   is reworked from the 2-way `object_key XOR volume` to a 3-way exactly-one. A `staged-path`
   entry seeds `state=registered`, `object_key=NULL`, `volume=NULL`, `path=<path>`, `digest=NULL`,
   `managed_by=config` (`reconcile_images._realize` gains a `StagedPathSource` branch).

3. **Resolution — wire the local-libvirt catalog rootfs lane (it is currently unwired).**
   `LocalLibvirtProvisioning.from_env()` (`composition.py:94`) builds the provisioner with **no**
   `catalog_fetch`, so today a `catalog` rootfs ref on local-libvirt raises *"catalog rootfs
   materialization is not wired for this lane"* (`materialize.py:106`) and `fetch_registered_rootfs`
   (`images/fetch.py`) has no production caller — local-libvirt advertises `catalog` rootfs support
   (`composition.py:51`) it cannot deliver. We wire it: a synchronous
   `rootfs_catalog_fetch_from_env(allowed_roots)` (mirroring `build_config_fetch_from_env`,
   `build_configs/defaults.py:33`) lazily opens a sync `psycopg` connection + object store per call
   — the provider seam is synchronous and runs off the event loop via `asyncio.to_thread`. It
   resolves the registered row and branches on the source column:
   - `path` (staged-path) → `validate_local_component_path(path, allowed_roots=…)` (re-checks
     absolute-ness, existence via `resolve(strict=True)` which also rejects symlink escape,
     `allowed_roots` containment, regular-file, readability) and returns it — **no object store,
     no cache, no digest.**
   - `object_key` (s3) → fetch the object, verify its sha256 against `digest`, cache it
     (`fetch_registered_rootfs`'s existing logic, made synchronous).
   The fetch resolves at **public scope** (`visibility = public`) and **matches the provisioning
   profile's `arch`** (the `catalog` ref carries no arch, and `resolve_rootfs` historically dropped
   it — so the fetch threads `profile.arch` and `resolve_public_rootfs_sync` filters on it, making the
   match deterministic via the `(provider, name, arch)` unique index). To keep public-scope honest, a
   `staged-path` image declared with `visibility = "private"` is **rejected at inventory load** — a
   private row would still surface to its owning project via the RBAC-scoped `images.list` yet be
   unresolvable by the public-scope seam (a discoverable-but-unprovisionable trap), so local
   staged-path is public-only by contract. Threading the owning project through the seam (to support
   private local images) is a follow-up.

4. **No new discovery surface.** `fixtures.list` / `images.list` query shapes are unchanged: a
   registered public `staged-path` row already surfaces by `(provider, name, arch)`, and the agent
   provisions with `{kind:"catalog", provider, name}`. The absolute `path` is resolution-internal
   and is **not** projected into any MCP response. `systems.profile_examples` likewise needs no
   change — `_public_image` already selects the first PUBLIC `[[image]]` for the provider, so a
   declared staged-path image makes the local example a real `catalog` ref with
   `uses_real_reference: true`.

## Consequences

- A pure-MCP agent discovers a local rootfs through the existing catalog surface and provisions
  it via the catalog lane — D1 closed with **no new tool, port, or response schema**. The
  local-libvirt walkthrough declares an `[[image]]` instead of instructing a host `ls`.
- Wiring the lane also closes the latent gap where local-libvirt advertised `catalog` rootfs
  support but raised "not wired for this lane": **both** s3-backed and staged-path catalog rootfs
  refs now resolve on local-libvirt. `fetch_registered_rootfs` becomes live production code.
- A registered staged-path row is **declared, not probed**: seeding sets `registered` with no
  filesystem existence check (no analog to the S3 HEAD that gates `pending → registered`), matching
  the remote `staged`/volume lane. Discovery may therefore advertise a `registered` staged-path
  image whose file is currently absent/unreadable/escaped; **resolution is the authoritative gate**,
  re-validating `allowed_roots` containment on every provision and failing closed as a
  `configuration_error`. The reconcile context is server-side without guaranteed provider-root
  filesystem access, so a seed-time probe is deliberately omitted.
- The path is validated against `allowed_roots` on **every** resolution (no-leak / no-escape,
  ADR-0123/0065). A staged-path row whose file drifts outside roots, vanishes, becomes a
  non-regular file, or is unreadable fails closed as a `configuration_error` at provision — not
  silently. Discovery never reveals the path itself, only `(provider, name)`.
- Cost is one migration plus one CHECK rework; `ImageCatalogEntry` gains `path: str | None`.
  Seeding gains a branch; resolution gains a branch threaded with `allowed_roots`.
- A `staged-path` row carries **no digest**. Integrity rests on filesystem trust within
  `allowed_roots` — the same trust model as today's `local` path lane (the path the operator
  already trusts) and the remote `staged` volume (also digest-less). This is documented, not a
  silent weakening: an in-place rebuild of an iterating local rootfs need not re-seed the catalog.

## Considered & rejected

- **Option A — a new `rootfs.list` tool** that scans `allowed_roots` and returns `local` refs.
  A second, provider-specific discovery surface parallel to the catalog, returning a different
  ref kind than every other discoverable image, that no other provider shares. Rejected for
  surface duplication: the catalog already exists for exactly this.
- **Option B — `systems.profile_examples` falls back to a discovered path.** Surfaces a single
  file buried inside one example and bolts a filesystem scan onto a tool that is otherwise a pure
  inventory read. Rejected as partial discovery in the wrong tool.
- **Option C1 — publish the local file to S3** and seed an `s3` catalog row. Fully uniform and
  content-addressed, but round-trips a multi-GB rootfs through the object store — the very cost
  local staging exists to avoid. Rejected as the default; the existing publish lane stays
  available for operators who want content-addressing.
- **Reuse the existing `staged` source + `volume` column to hold a path.** Zero migration, but
  overloads `volume` — a libvirt storage-pool volume name for remote — with filesystem-path
  semantics for local, forcing every reader (resolution, listings, future providers) to branch on
  `provider == "local-libvirt"`. Rejected for semantic overloading; an explicit `staged-path` /
  `path` keeps the two host-resident resolution modes type-distinct.
- **Resolve the path by re-reading `systems.toml` at materialize time** (no DB column). Breaks the
  `image_catalog`-row-as-single-source-of-truth invariant (ADR-0092) and couples worker resolution
  to inventory-file availability on the worker. Rejected.
- **Carry a digest on staged-path rows.** A path's bytes are mutable in place; a digest captured
  at seed time would rot and force a re-seed on every rebuild of a locally-iterating rootfs. The
  `local` lane and the remote `staged` lane are both digest-less for the same reason. Rejected as
  the default.
