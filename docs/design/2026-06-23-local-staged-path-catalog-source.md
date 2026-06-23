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
   **Declared-not-probed (honesty contract):** a staged-path entry seeds straight to `registered`
   with **no** filesystem existence probe — there is no analog to the S3 HEAD that gates
   `pending → registered` (`reconcile_images._realize` line 261, `StagedSource` returns `registered`
   unconditionally). This matches the remote `staged`/volume lane, which also seeds `registered`
   without probing the host volume. Consequence: discovery can advertise a `registered` staged-path
   image whose path is currently absent, unreadable, or escaped; **resolution (criterion 5) is the
   authoritative gate**, re-validated on every provision. The reconcile context is server-side and
   is not guaranteed filesystem access to the provider root, so a seed-time probe is deliberately
   out of scope — the catalog row is a *declaration*, the boot is the proof.
3. The seeded row is returned by `fixtures.list` (public) and `images.list` (RBAC) by
   `(provider, name, arch)`; neither response exposes the absolute `path`. **This no-leak is an
   invariant, not an accident:** `images.list` runs `SELECT *` then `ImageCatalogEntry.model_validate`
   (`images.py:39,103`), so the new `path` loads into every listed row in memory — the only thing
   keeping it off the wire is that `_row_envelope` (`images.py:50-64`) and `fixtures.list`'s
   projection are **explicit field allowlists** that omit `path`. The plan must keep both projections
   allowlist-shaped (no `**row`, no `path` field) and lock it with a regression test asserting no MCP
   response for a staged-path image contains the path string.
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
8. A public **s3**-backed `catalog` rootfs ref also resolves on local-libvirt (the previously-unwired
   lane): `from_env()` now supplies a `catalog_fetch`, so `materialize.py:106`'s "not wired for this
   lane" no longer fires for a registered public image. A non-existent name still fails closed as a
   `configuration_error`.

## Design

### Layers touched (top to bottom)

| Layer | File | Change |
|-------|------|--------|
| Inventory model | `inventory/model.py` | `StagedPathSource(kind="staged-path", path)`; add to `ImageSource` union; validate `path` absolute |
| Inventory serialize | `inventory/serialize.py` (+ tests) | round-trip the new source kind if serialization is field-explicit |
| Domain model | `domain/catalog/images.py` | `ImageCatalogEntry.path: str | None = None` |
| Migration | `db/schema/0047_image_catalog_staged_path.sql` | add `path text`; rework `image_object_present` CHECK to 3-way exactly-one |
| Seeding | `inventory/reconcile_images.py` | `_realize` returns `path`; `StagedPathSource` → `(registered, None, None, path, None, None)`; INSERT/UPDATE carry `path` |
| Sync resolver | `images/catalog.py` | add `resolve_public_rootfs_sync(conn, provider, name)` — the sync, public-scope twin of `resolve_rootfs` |
| Resolution lane | `images/fetch.py` | add a sync `fetch_registered_rootfs_sync(conn, store, allowed_roots, provider, name)` (or refactor) branching `path`→validate / `object_key`→S3-fetch+digest+cache |
| Fetch wiring | new `providers/local_libvirt/lifecycle/rootfs_catalog_fetch.py` (or `materialize.py`) | `rootfs_catalog_fetch_from_env(allowed_roots) -> CatalogFetch` mirroring `build_config_fetch_from_env`: lazy sync `psycopg.connect` + `object_store_from_env`, resolve+branch |
| Provisioner wiring | `providers/local_libvirt/lifecycle/provisioning.py`, `composition.py` | `from_env()` wires `catalog_fetch=rootfs_catalog_fetch_from_env(self._allowed_roots)`; `_materialize_rootfs_base` passes `catalog_fetch=self._catalog_fetch` into the context |
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

### Resolution contract (wiring the unwired lane)

**Current state:** the local-libvirt `catalog` rootfs lane is unwired. `from_env()`
(`composition.py:94`) builds the provisioner with no `catalog_fetch`, so `_materialize_rootfs_base`
(`provisioning.py:342-352`) sets `catalog_fetch=None` and `materialize.py:106` raises *"catalog
rootfs materialization is not wired for this lane"*. `fetch_registered_rootfs` (`images/fetch.py:74`)
has **no production caller** (tests only). Local-libvirt advertises `catalog` rootfs support
(`composition.py:51`) it cannot deliver. C2b wires the lane.

**Seam (mirrors the build-config precedent).** `CatalogFetch = Callable[[CatalogComponentRef], Path]`
is **synchronous** (the provider seam runs off the event loop via `asyncio.to_thread`).
`rootfs_catalog_fetch_from_env(allowed_roots)` returns such a callable that, per call, lazily opens a
sync `psycopg.connect(DATABASE_URL)` + `object_store_from_env()` — exactly like
`build_config_fetch_from_env` (`build_configs/defaults.py:33-59`). It:

1. Resolves the registered **public** row via `resolve_public_rootfs_sync(conn, provider, name)`
   (sync, public-scope twin of `resolve_rootfs`). No match → `configuration_error` "unknown
   registered rootfs catalog entry".
2. Branches on the source column:
   - `path` present (staged-path) → `validate_local_component_path(path, allowed_roots=allowed_roots)`
     and return the resolved path. **No object store, no cache, no digest.**
   - `object_key` present (s3) → fetch the object, verify sha256 against `digest`, cache it under a
     digest-keyed path (the existing `fetch_registered_rootfs` body, made sync).

`from_env()` wires `catalog_fetch=rootfs_catalog_fetch_from_env(self._allowed_roots)` and
`_materialize_rootfs_base` passes `catalog_fetch=self._catalog_fetch` into the context. The
provisioner already owns `allowed_roots` (`self._allowed_roots`, default `[ROOTFS_DIR]`), so
staged-path containment is enforced at resolution exactly as the `local` lane enforces it.

**Scope note (RBAC):** resolution is **public-scope only**. Local-libvirt's discoverable catalog
images are declared `PUBLIC`; a project-private catalog rootfs on local-libvirt resolves to "unknown
registered rootfs catalog entry" (honest miss, not a silent wrong image). Threading the owning
project through the sync seam is a follow-up if local multi-tenant private images ever land.

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
