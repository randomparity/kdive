# Spec — operator-attested provenance for s3 catalog images (#1065, P5)

- **Issue:** #1065 (BLACK_BOX_REVIEW P5)
- **ADR:** [`../adr/0323-attest-operator-provenance.md`](../adr/0323-attest-operator-provenance.md)
- **Date:** 2026-07-10

## Problem

`images.describe` returns `direct_kernel.status = "unverified"` and
`kdump.capability = "unverified"` for the shipped fedora/rocky/centos catalog images. The
capability signals read a build-recorded provenance operand (`boot_kernel_count`,
`makedumpfile_version`) and honestly degrade to `unverified` when it is absent
(`capability_signals.py:55-67,88-96`). Those operands are recorded only by KDIVE's own
build/publish pipeline. The shipped catalog images are declared `source.kind = "s3"`
(`systems.toml.example`), operator-baked externally, so their operands are never recorded and
the pre-provision capability check the docs advertise ("read `direct_kernel`/`kdump` first so a
multi-kernel image does not burn an allocation") produces no actionable signal.

`unverified` is the correct honest state for an un-characterized image — the P5 finding confirmed
`capability_signals.py` is not a defect. But an operator who baked the image *knows* its
`/boot` kernel count and makedumpfile version. There is no way to record that knowledge, and the
docs do not say `unverified` is normal for un-published/operator-staged images.

## Decision (Option B — see ADR 0323)

Let an operator **attest** the characterization operands for an `s3` catalog image in
`systems.toml`, synthesized by the reconciler into the row's `provenance`, but **tagged as
operator-attested rather than build-verified** so the honesty invariant P5 is about is preserved:
an agent must be able to tell an operator claim from a KDIVE-verified fact.

## Scope

### 1. Characterize (Option B)

- **DB (migration 0064):** add `image_catalog.provenance_attested boolean NOT NULL DEFAULT false`.
  Build/publish/sidecar-verified provenance keeps the default `false`; operator-attested
  provenance sets it `true`.
- **Inventory model (`ImageEntry`):** add an optional typed `attested` sub-table
  (`AttestedProvenance` with the two registered-signal operands: `boot_kernel_count: int | None`,
  `makedumpfile_version: str | None`). A model validator rejects `attested` on any source other
  than `s3` (the shipped-catalog case; `build` owns verified provenance, `staged-path` has the
  sidecar path). At least one operand must be set when `attested` is present.
- **Reconciler (`reconcile/images.py`):** for an `s3` source with `attested` declared, synthesize
  `provenance` from the declared operands and set `provenance_attested = true`. Change-detecting
  as today (removing an operand from the file updates the row; steady state is a clean no-op). An
  `s3` source without `attested` is unchanged (empty provenance, `false`). A `defined` (un-digested)
  s3 row still carries the attested provenance, so the pre-check is actionable before publish.
- **Domain model (`ImageCatalogEntry`):** add `provenance_attested: bool = False`.
- **Signals (`capability_signals.py`, render-only):** add a `basis` field to each signal block when
  its operand is **present** — `"operator_attested"` when `entry.provenance_attested`, else
  `"build_verified"`. The `unverified` branches (operand absent) are unchanged: attestation never
  changes *when* `unverified` is emitted, only how a present operand is labelled.
- **`images.describe`:** surface `data.provenance_attested`; document the signal `basis` field.
- **`systems.toml.example`:** attest the fedora-kdive-ready-44 default (single kernel, makedumpfile
  1.7.9) as the worked example.

### 2. Soften docs (design-independent)

`docs/guide/toolsets/images.md` and the capability-signals bullet in `docs/guide/agent-index.md`
(+ regenerated `_content` mirror): state that `unverified` is the normal/honest state for an
un-published or un-attested operator-staged/s3 image, and that the pre-check becomes actionable
once the operands are attested (operator) or published (KDIVE build).

## Non-goals

- Not changing `capability_signals.py`'s `unverified` emission logic.
- Not attesting `staged`/`build` sources (out of the P5 scope; `staged-path` already carries the
  build-fs sidecar).
- Not verifying an attested claim — it is operator-owned config, surfaced as `operator_attested`.

## Acceptance

- An `s3` image with `[image.attested]` in `systems.toml` reconciles to a row whose `provenance`
  carries the declared operands and `provenance_attested = true`; `images.describe` reports the
  signal `status`/`capability` with `basis = "operator_attested"`.
- A build/publish/sidecar image reports the same operands with `basis = "build_verified"`.
- An un-attested s3 image still reads `unverified` (unchanged).
- `attested` on a non-`s3` source is a load-time `InventoryError`.
- Migration test (mirrors `tests/db/test_migration_0061_*.py`) proves the column is absent before
  0064 and a `NOT NULL`/`false`-default boolean after.
- `just ci` green.
</content>
</invoke>
