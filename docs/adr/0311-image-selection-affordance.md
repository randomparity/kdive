# ADR 0311 — Agent-facing image-selection affordance

- **Status:** Accepted
- **Date:** 2026-07-04
- **Deciders:** kdive maintainers
- **Issue:** #1017 (reframed; `BLACK_BOX_REVIEW.md` Finding 2(a), Epic #1018)
- **Spec:** [image-selection-affordance-1017](../archive/superpowers/specs/2026-07-04-image-selection-affordance-1017.md)
- Extends ADR-0092 (image catalog), ADR-0124 (`profile_examples`), ADR-0286/0295
  (capability honesty invariant), ADR-0296 (staged-path provenance). Supersedes
  nothing; closes the original #1017 curated-`boot_kernel_count` ask as
  won't-fix-as-specified.

## Context

An AI agent driving the platform consistently picks one image
(`fedora-kdive-ready-43`) regardless of task fit. Investigation located the
cause precisely, and it is neither documentation nor the `direct_kernel`
provenance gap the issue was filed against.

`systems.profile_examples` emits one ready-to-edit local example whose rootfs is
the **first `PUBLIC` `[[image]]` in `systems.toml` declaration order**
(`_public_image`, first match wins). An operator whose inventory declares
`fedora-kdive-ready-43` first therefore hands every agent that exact name as the
worked example, which the agent edits and reuses. `images.list`/`fixtures.list`
order alphabetically by `(provider, name, arch)`, so a list-ordering cause would
surface *centos* first — confirming `profile_examples`, not list order, drives
the convergence.

Compounding it, nothing lets an agent choose on merit: `images.list` returns
identity and publish state only (no capability data), so comparison needs an N+1
`images.describe` fan-out, and no surface says "when to choose X over Y". With no
basis to override the example, the agent doesn't.

No agent-visible surface names any image (zero image-name literals under
`src/kdive/mcp/`); the over-featuring of `fedora-kdive-ready-43` lives only in
internal operator docs an agent never reads.

The filed ask — curate a static `boot_kernel_count` so the shipped fixture reads
a definite `direct_kernel` verdict — reproduces the drift-prone static column
ADR-0295/0296 rejected, and would be confidently wrong for the multi-kernel
`fedora-kdive-ready-43` (`provisionable` when the honest answer is
`not_provisionable`). It is closed as won't-fix-as-specified.

## Decision

Give the agent honest, structured, per-image information to select on merit, and
stop `profile_examples` presenting one image as the default. Advertise facts and
operator context; never compute a ranking or a curated capability value.

1. **`images.list` carries comparison facts.** The list row envelope gains
   `capabilities` (the existing build-fact tag vocabulary, already stored and
   `SELECT *`-ed — no SQL/migration), a compact `os` identity, and an operator
   `description`. One `images.list` call now supports merit-based comparison
   without an N+1 `describe` fan-out. `capabilities` matches the key
   `images.describe` already emits.

2. **`/etc/os-release` is captured as a build fact.** The local-libvirt build
   plane probes `/etc/os-release` from inside the built image (guestfish, the
   same offline mechanism as the boot-entry probe, with the `/usr/lib/os-release`
   symlink fallback), parses `ID`/`VERSION_ID`/`PRETTY_NAME`, and records
   `provenance["os_release"] = {id, version_id, pretty_name}`. The capture is
   **advisory**: any probe failure, missing file, or unparseable body degrades to
   omission, so a degraded build's row stays byte-identical to a pre-feature one
   (the ADR-0252/0253/0295 contract). It flows through the existing
   `RootfsBuildOutput.provenance` → `publish_image` and staged-sidecar →
   reconcile paths (#977/ADR-0296) with **no migration**. `describe` surfaces the
   full record; `list` a compact form. An unbuilt row omits it — never a
   fabricated value.

3. **An operator `description` channel, reconciled to the row.** `ImageEntry`
   gains `description: str = ""` (mirroring `BuildConfigEntry.description`);
   `image_catalog` gains a nullable `description` column (migration `0060`);
   reconcile plumbs it through `_create_entry`/`_update_entry` exactly as
   `capabilities` is, so editing `systems.toml` and re-reconciling updates the
   row. It is length-capped at inventory-load (280 chars) so the field stays
   token-safe on every paginated `images.list` row. It is **reconcile-owned**:
   `publish_image` omits `description` from its write set today
   (`publish.py:188-192`) and must never be extended to write it, so a build
   never clobbers operator context. It is surfaced in `images.list`,
   `images.describe`, and the `profile_examples` example, **labelled
   operator-attested** — advisory context, never a capability or liveness
   guarantee (parallels ADR-0092's `client_attested` provenance and the #867
   client labels). Rows with no matching inventory `[[image]]` carry no
   description.

4. **`profile_examples` de-anoints the first-declared image.** It still emits one
   runnable example, but the local example item now discloses the image was
   chosen **by declaration order**, reports `available_images` (the count of
   public local-libvirt images), echoes the chosen image's `description`, and
   points the agent to `images.list` to choose deliberately. Reframes the example
   from *the* default to *an* example.

5. **Agent guidance, no image names.** The `toolsets-images.md` MCP resource
   gains a short "choosing an image" section: compare on `capabilities` (tag →
   task), read `os` for the target release, treat `description` as operator
   context — keeping the resource inventory-neutral.

This is **orthogonal to the `direct_kernel` signal**: no curated
`boot_kernel_count`, and unbuilt fixtures stay honestly `unverified`
(ADR-0228/0286).

## Consequences

- An agent can compare every image's tooling, verified OS release, and operator
  context in one `images.list` call and select on merit, instead of reusing the
  `profile_examples` default.
- Operators gain a first-class way to steer agents toward the images they curate
  (e.g. RHEL/SLES debug setups) without touching code — a `description` in
  `systems.toml`, reconciled to the catalog.
- `os_release` adds a verified, falsifiable OS identity to build provenance,
  cross-checking a possibly-mislabelled catalog name.
- One migration (`0060`, additive nullable column), shipped with the code and
  applied as part of the deploy via the advisory-lock-guarded `apply_migrations`
  step (ADR-0015). Reads tolerate either side of the migration (the model field
  defaults to `None`), but reconcile *writes* the column, so the migration must
  precede write traffic — hence migration-with-deploy, not code-strictly-first.
  This self-hosted control plane is not a hot rolling multi-instance tier, so the
  only skew window (old code reading a migrated DB, rejected by `extra="forbid"`)
  is the brief deploy restart. Migrations are forward-only. The `images.list`
  output contract grows (additive fields); fielded-output/snapshot tests are
  updated. No RBAC change (all fields respect the existing public/private list
  filter).
- Honesty is preserved: every new field is either a build fact or explicitly
  operator-attested; no ranking or recommendation is computed, and no unbuilt
  row gains a fabricated capability value.

## Alternatives rejected

- **Curate a static `boot_kernel_count` (the original ask).** Reproduces the
  drift-prone write-only column ADR-0295/0296 rejected, does not flow into
  `image_catalog.provenance` from `rootfs_catalog.toml` anyway, and is
  confidently wrong for the multi-kernel `fedora-kdive-ready-43`. The honest
  gap for a *built* image is already closed (ADR-0296); an unbuilt row's
  `unverified` is correct (ADR-0228).
- **A computed suitability score / ranking / "best image for task X".**
  Re-introduces the editorialising ADR-0286/0295 exist to prevent, depends on
  the unverified provenance signals for unbuilt fixtures, and needs a task-intent
  vocabulary. The design surfaces facts and lets the agent decide.
- **Have `profile_examples` pick the "best" image instead of first-declared.**
  Still a silent ranking, and fragile. Disclosing that the pick is by
  declaration order and pointing to `images.list` is honest and lets the agent
  choose.
- **Store the operator `description` in the `provenance` jsonb.** `provenance` is
  build-plane-authoritative and realized from the build/sidecar; mixing
  operator-attested freeform into it muddies the honesty boundary and risks being
  overwritten on reconcile. A dedicated reconciled column keeps the two
  provenances distinct.
- **Keep the operator `description` inventory-only (surface via
  `profile_examples` alone).** `images.list`/`describe` read the catalog row, not
  the inventory, so the hint would be invisible exactly where an agent compares
  images. Reconciling to the row is the idiomatic path (as `capabilities` is).
- **Add os-release capture for all providers.** Only the local-libvirt build
  plane builds and probes images here; remote/s3 rows are published elsewhere and
  out of scope.
