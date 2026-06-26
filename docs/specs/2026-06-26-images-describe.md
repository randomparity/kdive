# Spec: `images.describe` + build-time package-version provenance (#829)

- Date: 2026-06-26
- ADR: [ADR-0252](../adr/0252-images-describe.md)
- Status: Draft

## Problem

Two gaps, one read surface:

1. **No per-image detail read.** `images.list` is the only image catalog tool. It projects a
   7-field subset of each row (`provider, name, arch, visibility, owner, state, volume`;
   `images.py:50`) and drops the rest of `ImageCatalogEntry`: `format`, `root_device`,
   `digest`, `capabilities`, `provenance`, `expires_at`, `managed_by`. An agent selecting a
   rootfs cannot inspect an image's boot layout, build provenance, or declared capabilities
   before committing to it — it picks by name/convention or learns from a post-provisioning
   failure. The data is on the row; the agent just cannot reach it.

2. **Provenance records package *names*, not versions.** Both build planes write
   `provenance["packages"]` as the *requested* package-name list (`spec.packages`;
   `local_libvirt/rootfs_build.py:318`, `remote_libvirt/rootfs_build.py:171`). What actually
   got installed — and at what version — is never captured. An agent that can read provenance
   (via gap 1) still cannot tell which drgn/kexec-tools/makedumpfile version an image carries
   (makedumpfile is a directly requested package on the default Fedora/Debian debug images; on
   EL8/EL9 it is bundled in kexec-tools — see Coverage of capability tooling below).

This is the foundation for a dependent follow-up (#830) that surfaces a computed kdump
capability per image; that computation needs machine-readable installed versions, which gap 2
supplies and gap 1 exposes.

## Goal

- Add `images.describe`, a read-only, RBAC-filtered MCP tool returning one image's full
  agent-relevant detail (including `provenance`), addressed by catalog row id.
- Capture installed package versions at build time into a new additive
  `provenance["package_versions"]` field, so `images.describe` advertises versions, not just
  requested names.

## Non-goals

- No write path on the read tool, no schema change, no migration (the `provenance` column is
  schemaless jsonb; the new field is additive within it).
- No reshape of the existing `provenance["packages"]` list (kept for backward compatibility;
  versions land in a separate `package_versions` map).
- No new RBAC role; reuse the `images.list` visibility predicate.
- No computed capability (kdump etc.) — that is #830, which builds on this.
- No triple-addressed lookup or `resolve_rootfs` shadow semantics (see ADR rejected).
- No backfill of versions onto already-built images; only new builds populate
  `package_versions` (describe omits the field for older rows — backward compatible).

## Decision summary (see ADR-0252)

Address by catalog row `id` (UUID), mirroring `resources.describe(resource_id)`. The
`(provider, name, arch)` triple is only partially unique (a public and a shadowing private
row can share it; `pending` rows are unconstrained), so it cannot deterministically address
one row; the UUID is the only total key and `images.list` already returns it as each item's
`object_id`.

## Behavior

`images.describe(image_id: str) -> ToolResponse`

1. Parse `image_id` as a UUID. Malformed → `configuration_error` (parse failure), byte-shape
   consistent with the existing `invalid_uuid_error`/`config_error` helpers.
2. Fetch the row by id. RBAC: the row is visible iff it is `public`, or `private` and owned
   by a project where the caller's token satisfies `viewer` (the exact `images.list`
   predicate, `owner = ANY(viewer-projects)`). The filter is applied in SQL so an
   unauthorized private row never leaves the database.
3. A valid UUID with no visible row → `not_found` (byte-identical whether the row is absent
   or merely invisible — no existence leak, no membership leak).
4. A visible row → a single `ToolResponse.success` envelope, object id = the row id, status =
   the publish `state`, with `data` carrying the projected fields below.

### Field projection

`data` carries, in addition to the `images.list` subset:

| field | source | notes |
|-------|--------|-------|
| `provider`, `name`, `arch` | row | natural identity |
| `format`, `root_device` | row | boot layout |
| `visibility`, `owner`, `state` | row | scope + publish state (`owner` → `""` when public) |
| `digest` | row | qcow2 content digest; `""` until built |
| `capabilities` | row | declared capability tags (list) |
| `provenance` | row | build-metadata jsonb, surfaced verbatim |
| `volume` | row | staged provider volume token; `""` when none |
| `expires_at` | row | private-image reclaim deadline; ISO-8601 string when set, `""` when public/none (`datetime` is not a `JsonValue`, so it is serialized via `isoformat()`) |
| `managed_by` | row | `config` vs `runtime` provenance of the row |

### Withheld (no-leak)

- `path` — the absolute staged host file path. `images.list` already withholds it
  (ADR-0123/0228, no-leak); `describe` must too. Asserted by test (the secret string never
  appears anywhere in the response dump).
- `object_key` — the S3 storage locator. Internal storage addressing, not requested by the
  issue, and not agent-actionable; withheld to keep the surface minimal.

`provenance` is build metadata (distro, releasever, package names, the new `package_versions`
map, source digest, layout, readiness marker, guest MAC, authorized-key *name*). It holds no
secret *values* — secrets in this system are by-reference and resolved at the worker boundary
— so it is surfaced verbatim. (A future provenance field that embedded a host path or secret
would need to be filtered at its write site, not here; that is out of scope.)

## Build-time package-version capture

After the image is built and packages are installed — on the customized `scratch` (local,
before the ext4 repack) and on the `virt-builder` output (remote) — the plane inspects the
image read-only and records the installed version of each requested package.

- **Seam.** A shared, package-manager-neutral inspection function in
  `images/planes/_build_common.py`, injected into each plane's tools dataclass and defaulting
  to a real implementation, mirroring the `images/validation.py` `InspectSeam` pattern so unit
  tests inject a fake and need no libguestfs. The real implementation runs `virt-inspector`
  (read-only, no guest code execution, neutral across rpm/dpkg), parses the XML
  `<application><name>/<version>` entries, and returns `{name: version}`.
- **Projection.** Each plane filters the full installed map to **the same package set it
  records in `provenance["packages"]`** — `spec.packages` for local, and
  `_guest_agent_packages(spec.packages)` for remote (which always injects `qemu-guest-agent`,
  so its version is captured too, keeping the two lists consistent). It writes
  `provenance["package_versions"] = {name: version}` for those resolved. A requested name with
  no matching installed application is simply absent from the map (discoverable: it is in
  `packages` but not `package_versions`); `provenance["packages"]` is unchanged.
- **Coverage of capability tooling.** The default debug images carry their kdump/drgn tooling
  as *directly requested* packages — Fedora (`DEFAULT_DEBUG_FS_PACKAGES`) and Debian
  (`_DEBIAN_DEBUG_PACKAGES`) both list `makedumpfile` explicitly — so its version is captured
  by this mechanism. The one exception is EL8/EL9, which bundle `makedumpfile` inside
  `kexec-tools` with no standalone package (`rhel.py:42`); there `package_versions` records the
  `kexec-tools` version, and the makedumpfile *binary* version (not a package version anywhere)
  is left to #830's binary-probe work. #829 guarantees versions for the requested package set,
  not for sub-dependencies.
- **Failure posture: degrade, do not fail the build.** Version capture is advisory provenance
  enrichment, not a build-correctness invariant (unlike `virt-customize`/repack, which must
  fail hard). If the inspector is absent or errors, the plane logs a WARNING and publishes the
  image with `package_versions` **omitted** — never a half-built image and never a build
  regression for an otherwise-good image. This matches the degrade-don't-fail posture of the
  staged-volume probe and capability projection (ADR-0194). An omitted map is byte-identical
  to an older image's (no versions captured); the build log is the place to diagnose a broken
  inspector. This means a reader cannot distinguish "capture failed" from "image predates this
  feature" from a row alone — an accepted limitation for #829, where `package_versions` is
  advisory. A consumer that must make a *decision* on these versions (#830) owns adding any
  machine-readable capture-reliability signal it needs; #829 does not add one speculatively.

## CLI parity

`resources describe` is a curated CLI verb (`registry.py:57`,
`reads.resources_describe`). Convention therefore requires `kdivectl images describe
<image_id>`: a new `reads.images_describe` mirroring `resources_describe` (the generic
`_record` single-record path) plus a `Verb("images", "describe", reads.images_describe,
"images.describe", ("image_id",))` registry entry.

## Affected surfaces (the full change set)

Read tool:

- `src/kdive/mcp/tools/catalog/images.py` — `describe_image` handler + `images.describe`
  registration (`read_only`, `maturity="implemented"`).
- `src/kdive/cli/commands/reads.py` — `images_describe` verb.
- `src/kdive/cli/commands/registry.py` — the `images describe` Verb entry.
- `tests/mcp/test_read_tools_annotated.py` — add `images.describe` to `READ_TOOLS`.
- `docs/guide/reference/images.md` — regenerated tool reference (CI `docs-check` gate).
- Tests: handler RBAC/projection (`tests/mcp/catalog/`), CLI verb (`tests/cli/`).

Build-time version capture:

- `src/kdive/images/planes/_build_common.py` — shared `inspect_package_versions` seam
  (`virt-inspector` real impl) + `DEFAULT_VERSION_INSPECT`.
- `src/kdive/providers/local_libvirt/rootfs_build.py` — inject the seam into
  `RootfsBuildTools`, call it on the customized scratch, add `package_versions` to
  `_provenance`.
- `src/kdive/providers/remote_libvirt/rootfs_build.py` — inject the seam into
  `RemoteRootfsBuildTools`, call it on the virt-builder output, add `package_versions` to
  `_provenance`.
- Tests: `_build_common` version parse/filter/degrade; both planes' provenance assertions
  updated for `package_versions`.

## Exit criteria (falsifiable)

1. `images.describe(<id of a public row>)` returns that row with `capabilities`,
   `provenance`, `format`, `root_device`, `digest`, and `state` present.
2. A `viewer` on the owning project sees their own `private` row; the byte-identical
   `not_found` is returned for (a) an unauthorized private row, (b) an unknown-but-valid
   UUID. The two responses are indistinguishable.
3. A malformed `image_id` returns `configuration_error`, not `not_found`.
4. A staged-path image's absolute `path` never appears in the response; `object_key` is
   absent from `data`.
5. `kdivectl images describe <id>` renders the record and maps a server denial / not-found
   to the documented non-zero exit code.
6. `just docs-check` is clean (the generated `images.md` includes `images.describe`).
7. A build with requested packages `[p1, p2]` whose inspector reports versions records
   `provenance["package_versions"] == {"p1": "<v1>", "p2": "<v2>"}`; `provenance["packages"]`
   is unchanged.
8. A requested package the inspector does not report is absent from `package_versions` (not a
   null/empty value), while still present in `packages`.
9. An inspector that raises `MISSING_DEPENDENCY`/`INFRASTRUCTURE_FAILURE` leaves the build
   succeeding with `package_versions` omitted (degrade-don't-fail), and logs a WARNING.
10. Live (`live_vm`): a real local build of a debug image records real installed versions for
    its kdump/drgn tooling in `package_versions`, surfaced through `images.describe`.
