# ADR 0296 — Persist build provenance on the local staged-path reconcile flow

- **Status:** Accepted
- **Date:** 2026-07-02
- **Deciders:** kdive maintainers

## Context

The computed capability signals `direct_kernel` (operand `provenance["boot_kernel_count"]`,
ADR-0295) and `kdump` (operand `provenance["makedumpfile_version"]`, ADR-0253) render a confident
answer only when the `image_catalog` row carries the operand in its `provenance` column. ADR-0295
recorded the gap it left open: the operand lands only on the provenance-carrying `publish_image`
path, so local-libvirt staged fixtures always read `unverified`. This ADR closes that gap.

There are two catalog-population paths and only one carries provenance:

- **`publish_image` (S3-backed) carries it.** `IMAGE_BUILD` and the private-upload path pass
  `RootfsBuildOutput.provenance` into `publish_image`, which persists the `provenance` column
  (`_insert_pending` writes it via `Jsonb`). Both signals are confident there.
- **Inventory reconcile (local staged path) drops it.** `staged-path` fixtures such as
  `fedora-kdive-ready-43` are declared in `systems.toml` and registered by
  `inventory/reconcile/images.py`; `_create_entry`/`_update_entry` set every realized field
  **except** `provenance`, so the row keeps the schema default `{}`. `build-fs`
  (`run_build_fs`) computes `output.provenance` and then discards it.

So on the exact `fedora-kdive-ready-43` fixture #954 targets, both `boot_kernel_count` and
`makedumpfile_version` are absent and both signals read `unverified` permanently, regardless of
rebuilds.

See `docs/superpowers/specs/2026-07-02-local-staged-provenance-977.md`.

## Decision

Bridge the deliberately-decoupled `build-fs`→reconcile flow with a **build-fs provenance sidecar**
the reconcile picks up, so a locally-staged row carries the same provenance the publish path
already persists.

- **`build-fs` writes `<dest>.provenance.json`.** After `_publish_rootfs` moves the built qcow2 to
  `--dest`, `run_build_fs` writes a sidecar JSON beside it: `{"schema":
  "kdive.staged-provenance.v1", "digest": <output.digest>, "provenance": <output.provenance>}`.
  The `provenance` payload is `RootfsBuildOutput.provenance` verbatim. Written atomically (temp file
  + `os.replace`) so a concurrent reconcile never reads a partial file. **Advisory:** a sidecar-write
  failure logs a warning and does not fail the build — the qcow2 is the primary artifact, matching
  the "advisory capture never fails a build" stance of the makedumpfile/boot-count captures.

- **Reconcile persists the sidecar on a `staged-path` row.** `reconcile_images` resolves the
  sidecar off the event loop (like the existing s3 HEAD) via a `_resolve_staged_provenance` step
  and threads the result into realization. The `provenance` column is written by `_create_entry`
  and change-detected + written by `_update_entry`. Per source kind: `staged-path` adopts a valid
  sidecar's inner dict (else preserves the existing row provenance); `staged`/`build`/`s3` preserve
  the existing row provenance unchanged (a new row seeds `{}`).

- **Both operands travel together.** The sidecar carries the whole provenance dict, so
  `boot_kernel_count` and `makedumpfile_version` — and any future operand — reach the row together;
  `kdump` and `direct_kernel` cannot diverge on the same row. A staged-path row gets byte-identical
  provenance to the same image published via S3.

- **Honesty invariant preserved (ADR-0286).** A `staged-path` row with no sidecar, or a
  malformed/unknown-schema one, keeps its existing provenance (`{}` for a new row) and reads
  `unverified` — never a stale confident answer. An absent sidecar never wipes a populated row.

- **No re-hash, no content gate.** Reconcile does not re-hash the qcow2 against the sidecar's
  `digest` (hashing a multi-GiB file every pass is unacceptable; `staged-path` is declared-not-probed
  by ADR-0228 — provision-time resolution is the content gate). The recorded `digest` is provenance
  for audit, not a gate reconcile enforces.

No schema/migration (the `provenance jsonb NOT NULL DEFAULT '{}'` column exists since 0023), no
tool, RBAC, or config change. Tool visibility is unchanged.

## Consequences

- An agent reading `images.describe` `data.capability_signals` for a rebuilt local `staged-path`
  fixture now sees a confident `direct_kernel` and `kdump` answer instead of `unverified`, closing
  the ADR-0295 follow-up gap for the motivating `fedora-kdive-ready-43` case.
- Reconcile gains one small local-file read per `staged-path` row (off the event loop). It does not
  gain a `guestfish`/libguestfs dependency, and the drift loop stays a fast DB pass.
- `staged` (libvirt-volume) sources name no host path, so they have no natural sidecar location and
  stay `unverified`. Documented as out of scope; a future volume-probe or export-time sidecar could
  close it.
- An operator who replaces a staged qcow2 out-of-band without rewriting the sidecar carries the old
  provenance until the next `build-fs`; this is bounded by the same declared-not-probed contract
  that already governs staged-path content (ADR-0228), and the recorded `digest` lets a manual audit
  detect it.
- The build gains one small advisory write; a write failure degrades to an omitted sidecar, so it
  never fails a build. Unit tests drive the sidecar write, the read/degrade paths, and the reconcile
  persistence/change-detection with real temp files; an end-to-end local build+reconcile recording
  the operand is the operator-run live-stack path (the ADR-0285 stance).

## Alternatives considered

- **Probe the staged qcow2 at registration** (`probe_boot_entries` + `probe_makedumpfile_marker` in
  reconcile). Rejected: couples the reconcile/drift loop to `guestfish` and adds a slow libguestfs
  launch per row to a fast DB loop; recovers only the two probe-able operands, so a locally-staged
  row would carry a thinner, *different* provenance than the same image on the publish path — the
  divergence the sidecar avoids. Future operands would need reconcile changes each time.
- **Write provenance to the DB directly from `build-fs`.** Rejected: `build-fs` is a local CLI that
  does not talk to Postgres; coupling it to the DB breaks the build/reconcile decoupling the sidecar
  respects. The sidecar bridges the two over the filesystem they already share.
- **Re-hash the qcow2 in reconcile to gate on the sidecar `digest`.** Rejected: hashing a multi-GiB
  file every reconcile pass is unacceptable and duplicates the declared-not-probed contract
  (ADR-0228); a stale sidecar is bounded by that same contract.
- **Adopt the sidecar authoritatively (wipe provenance when the sidecar disappears).** Rejected: a
  transiently-unreadable or removed sidecar would regress a good row to `unverified`. Preserve on
  absence instead; only a present, valid sidecar changes the row.
- **A hand-curated static column / catalog bit.** Rejected for the same reason ADR-0253/0286 removed
  the write-only kdump bit: it drifts from the built image. The sidecar is derived from the build.
