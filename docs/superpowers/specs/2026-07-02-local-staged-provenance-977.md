# Persist build provenance on the local staged-path reconcile flow (#977)

- **Issue:** #977 (follow-up to #954 / PR #976, ADR-0295)
- **ADR:** [0296](../../adr/0296-local-staged-provenance.md)
- **Date:** 2026-07-02
- **Status:** Draft

## Problem

The computed capability signals `direct_kernel` (operand `provenance["boot_kernel_count"]`,
ADR-0295) and `kdump` (operand `provenance["makedumpfile_version"]`, ADR-0253) render a
confident answer only when the catalog row carries the operand in its `provenance` column.
There are two catalog-population paths and only one carries provenance:

- **`publish_image` (S3-backed) carries it.** `IMAGE_BUILD` and the private-upload path pass
  `RootfsBuildOutput.provenance` into `publish_image`, which persists it (`_insert_pending`
  writes the `provenance` column via `Jsonb`). Both signals compute a confident answer here.
- **Inventory reconcile (local staged path) drops it.** Local fixtures such as
  `fedora-kdive-ready-43` are `staged-path` qcow2s declared in `systems.toml` and registered by
  `inventory/reconcile/images.py`. `_create_entry`/`_update_entry` set
  `capabilities`/`object_key`/`volume`/`path`/`digest`/`state` but **never** `provenance`, so the
  row keeps the schema default `{}`. Separately, `build-fs` (`images/rootfs_command.py:run_build_fs`)
  computes `output.provenance` and then **discards** it — it moves the qcow2 to a path, prints
  `KDIVE_GUEST_IMAGE`, and never persists provenance anywhere.

**Consequence:** for a locally-staged image, both `boot_kernel_count` and `makedumpfile_version`
are absent, so `direct_kernel` **and** `kdump` read `unverified` on the exact fixtures #954 is
about — permanently, regardless of rebuilds.

## Goal

Persist build provenance on the local staged-path flow so both computed signals report a
confident answer for local fixtures, while preserving the ADR-0286 honesty invariant (a row still
lacking provenance reads `unverified`, never a stale confident answer).

## Approach: a build-fs provenance sidecar the reconcile picks up

`build-fs` and reconcile are decoupled by design — the build CLI produces a qcow2 at a path, the
operator points `systems.toml` at it, and a later reconcile registers the row. They share nothing
but the filesystem. Bridge them with a **provenance sidecar file** written beside the qcow2:

1. **`build-fs` writes the sidecar.** After `_publish_rootfs` moves the built qcow2 to `--dest`,
   `run_build_fs` writes `<dest>.provenance.json` — a small JSON document carrying the build's
   `RootfsBuildOutput.provenance` dict verbatim plus the output content `digest` and a schema
   version. Written atomically (temp file + `os.replace`) so a concurrent reconcile never reads a
   half-written file. Advisory: a sidecar-write failure logs a warning and does **not** fail the
   build (the qcow2 is the primary artifact; consistent with the makedumpfile/boot-count captures
   that omit an operand rather than fail a build).

2. **Reconcile reads the sidecar when realizing a `staged-path` row.** For a `staged-path` source,
   reconcile reads `<path>.provenance.json` (off the event loop, like the existing s3 HEAD), and
   persists the inner `provenance` dict into the row's `provenance` column. A missing, unreadable,
   malformed, or wrong-schema sidecar degrades to "no sidecar" and reconcile **preserves** the
   row's existing provenance (so an absent sidecar never wipes a previously-populated row back to
   `unverified`, and an empty row stays `{}`).

Because the sidecar carries the **entire** `RootfsBuildOutput.provenance` dict, both operands
(`boot_kernel_count`, `makedumpfile_version`) — and any future operand the build records — reach
the row **together**, so `kdump` and `direct_kernel` never diverge on the same row. A staged-path
row registered locally then gets byte-identical provenance to the same image published via S3.

## Why sidecar, not probe-at-registration

The rejected alternative is having reconcile probe the staged qcow2 read-only
(`probe_boot_entries` + `probe_makedumpfile_marker`) at registration. Rejected because:

- It couples the reconcile / drift-repair loop to `guestfish` (a heavy, slow dependency the loop
  does not have today) and adds a multi-second-to-minutes libguestfs launch per staged row to a
  loop meant to be a fast DB pass.
- It recovers only the two probe-able operands, not the full build provenance (pinned inputs,
  `package_versions`, `source_image_digest`), so a locally-staged row would carry a *different,
  thinner* provenance than the same image on the publish path — the divergence the sidecar avoids.
- "Future operands come for free" holds only for the sidecar: a new provenance key flows through
  without touching reconcile.

## Contract details

### Sidecar format

Path: `<qcow2-path>.provenance.json` (append the suffix; do not replace `.qcow2`, so the sidecar is
unambiguously bound to a specific qcow2 filename). Content:

```json
{
  "schema": "kdive.staged-provenance.v1",
  "digest": "sha256:<hex>",
  "provenance": { "...": "the RootfsBuildOutput.provenance dict, verbatim" }
}
```

- `schema` is a version discriminator. Reconcile rejects an unrecognized `schema` (degrade to "no
  sidecar"), so a future format change is a detectable break, not a silent misparse. This is a
  cross-process on-disk wire format, so versioning it is warranted (not speculative).
- `digest` is the built qcow2's content digest (`output.digest`), recorded for human/audit
  inspection of which build the provenance describes. Reconcile does **not** re-hash the qcow2
  (hashing a multi-GiB file every reconcile pass is unacceptable, and `staged-path` images are
  declared-not-probed by ADR-0228 — provision-time resolution is the content gate). The digest is
  provenance, not a verification gate reconcile enforces.
- `provenance` is `RootfsBuildOutput.provenance` unchanged.

### Reconcile persistence

- `_load_config_rows` additionally selects the `provenance` column (needed for change-detection
  and for preserving existing provenance on non-staged rows).
- The realization computes a `provenance` value per source kind:
  - **`staged-path`:** the sidecar's inner dict if a valid sidecar is present, else the row's
    existing provenance (preserve; `{}` for a new row).
  - **`staged` (libvirt volume):** the row's existing provenance (`{}` for a new row). A volume
    source names no host path, so there is no natural sidecar location — it stays `unverified`,
    documented as out of scope for this change.
  - **`build` / `s3`:** the row's existing provenance, unchanged — provenance on these rows is
    owned by `publish_image`; reconcile must never clobber it.
- `_create_entry` INSERT includes the `provenance` column (`Jsonb`); `_update_entry` includes
  `provenance` in its change-detection set and its UPDATE, so a rebuild that changes the sidecar
  refreshes the row and a steady state stays a clean no-op.

No schema change is needed: the `provenance jsonb NOT NULL DEFAULT '{}'` column has existed since
migration 0023.

## Acceptance criteria

1. `build-fs` writes `<dest>.provenance.json` (schema `kdive.staged-provenance.v1`, the output
   digest, and the full provenance dict) after publishing the qcow2; a write failure warns and
   does not fail the build.
2. Reconciling a `staged-path` row whose qcow2 has a valid sidecar persists the sidecar's
   provenance into the row's `provenance` column.
3. `images.describe` for that row renders `direct_kernel` and `kdump` with a confident status
   (`provisionable`/`not_provisionable`; the kdump status per operand) instead of `unverified`.
4. A `staged-path` row with **no** sidecar (or a malformed/wrong-schema one) keeps `{}` (or its
   existing provenance) and reads `unverified` — the honesty invariant holds; no regression.
5. Reconcile never overwrites the `provenance` of a `build`/`s3` row that `publish_image` populated.
6. A rebuild that changes the sidecar refreshes the row's provenance on the next reconcile; a
   steady state is a clean no-op (no phantom drift).

## Out of scope

- The `direct_kernel` signal and its build-time capture (#954 / PR #976).
- The fail-closed baseline-kernel selection (ADR-0272) is intended and unchanged.
- `staged` (libvirt-volume) sources — no host-path sidecar location; they stay `unverified`.
- "A provision failure consumes the Allocation" (#560).
