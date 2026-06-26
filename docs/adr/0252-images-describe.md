# ADR-0252: `images.describe` + build-time package-version provenance (#829)

- Status: Accepted
- Date: 2026-06-26

## Context

`images.list` (ADR-0092/0093) is the only image catalog tool. It projects a 7-field subset
of each `ImageCatalogEntry` row — `provider, name, arch, visibility, owner, state, volume`
(`src/kdive/mcp/tools/catalog/images.py:50`) — and drops `format`, `root_device`, `digest`,
`capabilities`, `provenance`, `expires_at`, and `managed_by`. An agent choosing a rootfs
cannot see an image's boot layout, build provenance, or declared capabilities before
provisioning; it picks by name/convention or learns from a post-provisioning failure. The
data is on the row — there is just no per-image read that surfaces it.

`resources.describe` (ADR-0023) is the established per-object detail pattern: a read-only,
RBAC-filtered tool that takes a `resource_id` UUID and returns one envelope visible to the
caller, with a `not_found` (no existence leak) for an absent or invisible id. There is no
image analog.

This is also the foundation for #830 (a computed kdump capability per image), which needs a
pre-provisioning per-image view to land its toolchain facts.

### Provenance records names, not versions

Both build planes write `provenance["packages"]` as the *requested* package-name list
(`spec.packages`; `src/kdive/providers/local_libvirt/rootfs_build.py:318`,
`src/kdive/providers/remote_libvirt/rootfs_build.py:171`). The *installed version* of each
package is never captured. Surfacing provenance (above) therefore advertises names with no
versions — and #830's kdump-capability computation needs a machine-readable makedumpfile
version, which does not exist on the row today.

### The identity question

`image_catalog` is keyed by an `id` UUID primary key; `(provider, name, arch)` is the
*natural* identity but is only **partially** unique. The uniqueness indexes are all partial
(`src/kdive/db/schema/0023_image_catalog.sql`): one `registered` `public` row per triple,
one `registered` `private` row per `(owner, provider, name)`, one `defined` `public` row per
triple. So a single `(provider, name, arch)`:

- can match a `public` row **and** a caller-visible `private` row at the same time (the
  shadowing case ADR-0093 resolution relies on), and
- has no uniqueness constraint over `pending` rows.

Only the `id` UUID is a total key. `images.list` already emits it as each item's
`object_id` (`str(entry.id)`), so a list → describe flow has the UUID in hand.

## Decision

Add `images.describe(image_id: str) -> ToolResponse`, addressed by the catalog row `id`
UUID, mirroring `resources.describe` for shape, registration, and maturity:
`@app.tool(name="images.describe", annotations=read_only, meta={"maturity":
"implemented"})`.

Authorization reuses the `images.list` visibility predicate exactly: a row is visible iff it
is `public`, or `private` and `owner = ANY(viewer-authorized projects)`, filtered in SQL so
an unauthorized private row never leaves the database. Resolution order:

1. Parse `image_id` as a UUID; malformed → `configuration_error` (a parse failure, distinct
   from a valid-but-absent id), via the existing `invalid_uuid_error` helper shape.
2. `SELECT` the row by id under the same visibility `WHERE` clause as the list. No visible
   row → `not_found`, byte-identical whether the row is absent or merely invisible (no
   existence leak, no membership leak — the ADR-0097 `not_found` contract).
3. A visible row → `ToolResponse.success(id, state, data=…)`.

`data` carries the `images.list` subset plus `format`, `root_device`, `digest`,
`capabilities`, `provenance`, `expires_at`, and `managed_by`.

The absolute staged host `path` and the S3 `object_key` are **withheld**: `path` is the
no-leak storage locator `images.list` already withholds (ADR-0123/0228); `object_key` is the
internal S3 key, not agent-actionable and not requested. `provenance` is surfaced verbatim —
it is build metadata (distro, releasever, package names, source digest, layout, readiness
marker, guest MAC, authorized-key *name*) holding no secret values (secrets are
by-reference, resolved at the worker boundary).

CLI parity follows the `resources describe` precedent: `kdivectl images describe <image_id>`
→ a `reads.images_describe` verb over the generic single-record path, registered as
`Verb("images", "describe", …, "images.describe", ("image_id",))`.

### Build-time version capture

Capture installed package versions at build time and record them additively in
`provenance["package_versions"]`, so `images.describe` advertises versions:

- A shared, package-manager-neutral inspection seam in `images/planes/_build_common.py`,
  injected into each plane's tools dataclass and defaulting to a real implementation (the
  `images/validation.py` `InspectSeam` pattern, so unit tests inject a fake and need no
  libguestfs). The real implementation runs `virt-inspector` read-only (no guest-code
  execution, neutral across rpm/dpkg), parses the XML `<application><name>/<version>` entries,
  and returns `{name: version}`.
- Each plane inspects the built image after packages are installed — the customized `scratch`
  for local (before the ext4 repack, when the layout is a normal bootable OS disk),
  the `virt-builder` output for remote — filters the installed map to **the same package set
  it records in `provenance["packages"]`** (local: `spec.packages`; remote:
  `_guest_agent_packages(spec.packages)`, so the always-injected `qemu-guest-agent` version is
  captured too), and writes `provenance["package_versions"] = {name: version}`.
  `provenance["packages"]` (the requested-name `list[str]`) is **unchanged**; the new field is
  additive so no existing consumer of `packages` breaks and the `provenance` jsonb needs no
  schema/migration. Versions are captured for the requested package set; a sub-dependency with
  no standalone package (e.g. `makedumpfile` bundled in `kexec-tools` on EL8/EL9) is not
  separately versioned here — its binary-version probe is #830's concern.
- **Degrade, do not fail the build.** Version capture is advisory provenance enrichment, not a
  build-correctness invariant. If the inspector is absent or errors, the plane logs a WARNING
  and publishes the image with `package_versions` omitted — never a half-built image, never a
  build regression for an otherwise-good image (the degrade-don't-fail posture of the
  staged-volume probe / capability projection, ADR-0194). Only new builds populate the field;
  older rows omit it (backward compatible — describe simply has no `package_versions`).

No schema change, no migration, no new RBAC role, no env var. The MCP surface change is purely
additive, and the provenance change is additive within the existing schemaless jsonb column.

## Consequences

- An agent can inspect an image's full detail (boot layout, digest, capabilities,
  provenance) by id before provisioning, instead of from a post-failure.
- The list → describe handoff is uniform with `resources.list` → `resources.describe`
  (same id-addressing, same `not_found` no-leak contract).
- `images.describe` joins the read-tool guards: it must be added to `READ_TOOLS`
  (`tests/mcp/test_read_tools_annotated.py`) and the generated tool reference
  (`docs/guide/reference/images.md`, CI `docs-check`) regenerated.
- #830 has a per-image surface to extend with a computed capability, **and** the
  machine-readable installed versions (`package_versions`) that computation reads.
- Describe-by-id means an agent that only knows an image's name must `images.list` first to
  get the id — the same one-hop indirection `resources.describe` already imposes.
- Both build planes gain a `virt-inspector` step on the build host; it is read-only and runs
  only on a successful build. Builds on a host without `virt-inspector` keep succeeding (the
  field is omitted), so the new tool dependency is soft.
- `package_versions` is populated only for images built after this change; describe omits it
  for pre-existing rows. An operator who wants versions on an existing image rebuilds it.

## Considered & rejected

- **Address by `(provider, name, arch)` triple.** Reads more naturally but the triple is not
  unique: a public and a shadowing private row can share it, and `pending` rows are
  unconstrained, so it cannot deterministically address one row. Resolving by shadow
  semantics would conflate "describe this exact row" with "resolve what would boot" — a
  different question (that is `resolve_rootfs`).
- **Accept both an id and an optional triple.** Two addressing modes, two code paths, more
  tests and docs for a v1 read tool — premature surface area (YAGNI).
- **Surface `object_key` / `path`.** `path` violates the ADR-0123/0228 no-leak contract;
  `object_key` is internal storage addressing with no agent use. Both withheld.
- **A new RBAC role or visibility rule.** Unnecessary; the `images.list` predicate already
  expresses public + owned-private-with-viewer, which is exactly the describe scope.
- **A separate domain/repository method.** The list handler already reads `image_catalog`
  directly with a parameterized visibility clause; describe reuses the same SQL shape with a
  by-id predicate rather than introducing a repository abstraction for one reader.
- **Reshape `provenance["packages"]` into a name→version map.** Breaks the existing
  `list[str]` shape its consumers (and the provider build tests) depend on. A separate
  additive `package_versions` field preserves backward compatibility.
- **Make version capture a hard build gate.** Would regress build reliability — a transient
  `virt-inspector` failure would fail an otherwise-good image build — for what is advisory
  metadata. Degrade-don't-fail instead.
- **Capture the full installed-application set** (every package, not just requested). Bloats
  the `provenance` jsonb and the describe response with hundreds of entries the agent did not
  ask about; filter to `spec.packages`. (#830 may widen the captured set to specific
  capability tooling, e.g. makedumpfile, when it needs it.)
- **A per-family guest-command query** (`rpm -qa` / `dpkg-query`). Couples version capture to
  each family and executes guest binaries; `virt-inspector` is family-neutral and runs no
  guest code, and the remote plane has no family seam at all.
