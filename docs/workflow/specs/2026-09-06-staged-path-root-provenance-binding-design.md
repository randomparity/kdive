# Staged-path root provenance binding

Issue: #2216

Decision: [ADR-0624](../../adr/0624-bind-staged-path-catalog-to-inspected-root.md)

## Outcome

Let a new local-libvirt System acquire an immutable, mechanically inspected root authority from
an operator-staged qcow2 without rebuilding it, copying it through object storage, or manually
updating the database.

## Registration contract

`build-fs` writes `<image>.provenance.json` beside the staged qcow2. Inventory reconciliation
continues to bound that sidecar read at 64 KiB. It parses `provenance.root_spec` as the existing
closed `RootSpecV1` and promotes its `source.identity` only when all of these facts hold:

- the root specification is structurally valid;
- its architecture equals the inventory image architecture;
- its authority is `stage-inspection`; and
- its source kind is `staged-image`.

No image bytes are read during reconciliation. Missing root facts retain a registered row with a
path and no digest. Purported but unusable root facts retain no digest and produce an operator
warning.

An absent sidecar preserves established provenance. The digest is re-derived from that preserved
root specification, so changing the configured path alone cannot silently downgrade the row to an
unpinned path.

## Provisioning contract

The local catalog fetch resolves the registered public row by provider, name, and architecture.
For a staged path it applies the existing absolute-path, allowed-root, symlink, regular-file, and
readability checks. When the row has the promoted digest, the same validator streams the actual
file and requires an exact SHA-256 match before the provider can use it. It never constructs an
object store or cache for this path.

System admission resolves the eligible row through the existing root-provenance service. A
checksum-pinned local reference selects it by digest; a catalog reference selects it by provider,
name, and architecture. The resulting snapshot remains immutable and bound to the new System.

## Compatibility and lifecycle

Sidecarless staged paths keep the existing declared-path behavior. No catalog schema, inventory
schema, or provider request changes. Existing Systems retain exactly their original snapshot or
their original absence of one. To use the new root authority, provision a new System after the
image and sidecar have been staged and reconciled; reconciliation is not a repair operation.

## Verification

Focused tests cover eligible promotion, malformed and architecture-mismatched refusal, ordinary
sidecarless registration, preserved provenance across a path change, exact and mismatched byte
materialization, and a real Postgres reconciliation followed by root-provenance resolution.
