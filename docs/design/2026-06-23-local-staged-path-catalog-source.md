# Local-libvirt staged-path catalog source (#732)

- **Issue:** #732 (D1 of epic #736, black-box review follow-up)
- **ADR:** [0228](../adr/0228-local-staged-path-catalog-source.md)
- **Status:** Draft → implementing
- **Date:** 2026-06-23

## Problem

A pure-MCP agent (no host shell) cannot discover a *resolvable* rootfs to provision on
local-libvirt. The quick-start lane uses a `local` rootfs reference
(`{kind:"local", path:"/var/lib/kdive/rootfs/…"}`) pointing at a raw file the operator dropped
under the provider's `allowed_roots`. That file is registered in **no** index, so every MCP
discovery surface — `fixtures.list`, `images.list`, `systems.profile_examples` — comes back empty
or placeholder-only, and the agent is forced off-surface to `ls` the host (defect D1).

Remote-libvirt has no such gap: operators declare `[[image]]` entries that seed `image_catalog`
rows the discovery surfaces enumerate. The catalog is the project's single provider-neutral,
RBAC-aware discovery surface (ADR-0092/0093) and already spans non-S3 host-resident images via the
`staged` (volume) source. Local-libvirt simply has no path-shaped equivalent.

## Goal

Make an operator-staged local rootfs a first-class `image_catalog` entry, discoverable through the
**existing** catalog surface and resolvable through the **existing** `catalog` rootfs lane, with no
new MCP tool or response schema. See ADR-0228 for the decision and rejected alternatives
(`rootfs.list` tool, `profile_examples` fallback, S3 publish, `volume`-column reuse).

## Success criteria (falsifiable)

1. An operator can declare
   `[[image]] provider="local-libvirt" … source={kind="staged-path", path="/var/lib/kdive/rootfs/x.img"}`
   in `systems.toml`; it parses and round-trips through inventory serialization.
2. `reconcile_images` seeds that entry as a `registered`, `config`-managed `image_catalog` row with
   `path` set and `object_key`/`volume`/`digest` NULL. The DB `image_object_present` CHECK accepts
   it and rejects a non-`defined` row that sets two of `{object_key, volume, path}` or none.
3. The seeded row is returned by `fixtures.list` (public) and `images.list` (RBAC) by
   `(provider, name, arch)`; neither response exposes the absolute `path`.
4. `validate_rootfs_reference({kind:"catalog", provider:"local-libvirt", name:"x"})` passes when the
   image is declared (it is in `doc.image`).
5. Provisioning a `catalog` ref whose backing row is a staged-path image resolves to the validated
   `allowed_roots` path with no S3 fetch. Resolution fails closed as a `configuration_error` when
   the path is missing, non-regular, unreadable, or escapes `allowed_roots` (incl. a symlink that
   resolves outside).
6. `systems.profile_examples` emits the local example as a real `catalog` ref with
   `uses_real_reference: true` when a public staged-path image is declared (no code change — it
   already selects the first public `[[image]]`).
7. The local-libvirt walkthrough provisions from the MCP surface alone — no host `ls`.

## Design

### Layers touched (top to bottom)

| Layer | File | Change |
|-------|------|--------|
| Inventory model | `inventory/model.py` | `StagedPathSource(kind="staged-path", path)`; add to `ImageSource` union; validate `path` absolute |
| Inventory serialize | `inventory/serialize.py` (+ tests) | round-trip the new source kind if serialization is field-explicit |
| Domain model | `domain/catalog/images.py` | `ImageCatalogEntry.path: str | None = None` |
| Migration | `db/schema/0047_image_catalog_staged_path.sql` | add `path text`; rework `image_object_present` CHECK to 3-way exactly-one |
| Seeding | `inventory/reconcile_images.py` | `_realize` returns `path`; `StagedPathSource` → `(registered, None, None, path, None, None)`; INSERT/UPDATE carry `path` |
| Resolution | `images/fetch.py` (+ local-libvirt catalog_fetch wiring) | branch: row carries `path` → `validate_local_component_path(path, allowed_roots)` and return; else S3 fetch. Thread `allowed_roots` |
| Walkthrough | `examples/local-libvirt/…`, `systems.toml.example` | declare a staged-path `[[image]]`; drop the host `ls` step |

### Data shapes

Inventory (`systems.toml`):
```toml
[[image]]
provider = "local-libvirt"
name = "fedora-rootfs"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda"
visibility = "public"
source = { kind = "staged-path", path = "/var/lib/kdive/rootfs/fedora-rootfs.qcow2" }
```

Catalog row after seeding: `state=registered, object_key=NULL, volume=NULL, path=<path>,
digest=NULL, managed_by=config`.

Provisioning (what the agent pastes, discovered from `fixtures.list`):
```json
{ "kind": "catalog", "provider": "local-libvirt", "name": "fedora-rootfs" }
```

### Resolution contract

`fetch_registered_rootfs` (and the local-libvirt `CatalogFetch` wiring that builds it) resolves the
registered row, then branches on the source column the row carries:

- `object_key` present → existing S3 GET + digest check + cache (unchanged).
- `path` present → `validate_local_component_path(path, allowed_roots=<worker's roots>)` and return
  the resolved path directly. No object store, no cache, no digest.

The worker already constructs the materialization `allowed_roots`; the resolver is given the same
set, so containment is enforced at resolution exactly as the `local` lane enforces it.

### No-leak / safety

- Discovery exposes only `(provider, name, arch)` (+ the existing `volume`, which is NULL/empty for
  staged-path). The absolute `path` is never projected to an MCP response (verified by a test that
  asserts `images.list`/`fixtures.list` output carries no path).
- Every resolution re-validates `allowed_roots` containment (incl. symlink escape via
  `resolve(strict=True)`), so a row that drifts out of roots cannot resolve.
- No digest by design (ADR-0228 trust model): consistent with the `local` and remote `staged`
  lanes.

## Edge cases / failure modes

- **Path missing / non-regular / unreadable at provision** → `configuration_error` from
  `validate_local_component_path` (re-validated each time, not trusted from seed time).
- **Path escapes `allowed_roots`** (declared outside, or a symlink resolving outside) →
  `configuration_error` "outside provider allowed roots" with the `accepted_values` roots
  (ADR-0224 enumeration, already wired in `local_paths.py`).
- **Declared with two source fields** (e.g. malformed hand-edit) → pydantic discriminated-union
  rejects at parse; DB CHECK is the backstop for direct row writes.
- **Staged-path declared for a non-local provider** → out of scope; resolution is local-libvirt's.
  Remote keeps `staged`/volume. (Validation may warn/reject a `staged-path` on remote — decided in
  the plan.)
- **Empty / absent `systems.toml`** → no images seeded; `profile_examples` keeps its placeholder
  fallback; unchanged.

## Out of scope

- Auto-discovering undeclared files under `allowed_roots` (that is the rejected Option A). The
  operator declares the image; this is the deliberate registration step that keeps the catalog the
  single source of truth.
- Remote-libvirt staged-path (remote already has `staged`/volume).
- Content-addressing local rootfs (Option C1 / the existing S3 publish lane is untouched).
