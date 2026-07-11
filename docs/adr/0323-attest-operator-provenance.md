# ADR 0323 — Operator-attested provenance for un-built catalog images

- **Status:** Accepted
- **Date:** 2026-07-10
- **Deciders:** kdive maintainers
- **Spec:** [`../specs/2026-07-10-attest-s3-provenance-1065.md`](../design/2026-07-10-attest-s3-provenance-1065.md)
- **Follows:** [ADR-0286](0286-image-capability-metadata.md) — the computed-capability-signal
  honesty invariant this decision must preserve; [ADR-0296](0296-local-staged-provenance.md) —
  the staged-path sidecar that first fed non-publish provenance into a catalog row.

## Context

`images.describe`'s `direct_kernel` and `kdump` capability signals read a build-recorded
provenance operand (`boot_kernel_count`, `makedumpfile_version`) and degrade to `unverified` when
it is absent — the ADR-0286 honesty invariant, working as designed. The operands are recorded only
by KDIVE's own build/publish pipeline. The shipped catalog images (`fedora-kdive-ready-44`, the
rocky/centos family) are declared `source.kind = "s3"`: operator-baked externally, never built
through KDIVE, so they carry no operands and both signals read `unverified`. The pre-provision
capability check the docs advertise produces no actionable signal for the very images we ship
(BLACK_BOX_REVIEW P5).

The operator who baked such an image knows its `/boot` kernel count and makedumpfile version.
Nothing lets them record that knowledge. Two directions were possible: (a) publish/characterize the
shipped images through KDIVE so the operands are build-recorded, or (b) let the operator attest the
operands in `systems.toml`.

The tension is the honesty invariant. ADR-0296 already feeds non-publish provenance (a build-fs
sidecar) into the same `provenance` blob **with no distinguishing marker**. Extending that to an
operator-declared blob would let an operator claim `boot_kernel_count = 1` and have the signal read
`provisionable` indistinguishably from a KDIVE-verified fact. The sidecar is build-fs output that
inspected the actual image; an operator attestation is an unverified claim. Collapsing the two
would let a wrong attestation read as a verified capability — exactly the false-confidence failure
the signals exist to prevent.

## Decision

We will let an operator attest the two registered-signal operands for an `s3` catalog image via an
optional typed `[image.attested]` sub-table, and record a typed
`image_catalog.provenance_attested` boolean marking the row's provenance as operator-attested
rather than build-verified. The reconciler synthesizes the declared operands into `provenance` and
sets the marker; the capability signals gain a `basis` field (`operator_attested` |
`build_verified`) on each **present**-operand block, so an agent always sees whether a confident
signal rests on an operator claim or a KDIVE-verified fact. `unverified` (operand absent) is
unchanged. We separately soften the images/agent-index docs to state `unverified` is the normal
honest state for an un-published/un-attested image.

## Consequences

- The pre-provision capability check becomes actionable for the shipped catalog without a build:
  an operator attests once in `systems.toml` and the signals report `provisionable`/`capable` with
  `basis = "operator_attested"`.
- The honesty invariant is preserved and *strengthened*: the `basis` field now discloses the
  provenance origin even for build-verified images, so a confident signal is never ambiguous about
  its evidence.
- New obligations: a forward-only additive migration (0064); a typed inventory model + a validator
  restricting `attested` to `s3` sources; a new agent-facing field (`basis`, `provenance_attested`)
  documented on `images.describe`.
- Attestation is trusted operator config, not verified. A wrong attestation yields a wrong
  `operator_attested` signal — but it is labelled as a claim, and the operator owns `systems.toml`.
- Scope is deliberately narrow: only `s3` sources, only the two registered operands. `staged-path`
  keeps its sidecar; `build` keeps publish-owned provenance; neither accepts `attested`.

## Alternatives considered

- **(a) Publish/characterize the shipped images through KDIVE.** Makes the operands genuinely
  build-verified, the strongest honesty. Rejected as the primary path: it forces every operator to
  run the images through a build/probe step before the catalog is usable, and the shipped images
  are externally baked by design (`s3`, no build recipe). Attestation is the lightweight bridge;
  publishing remains available and reads as `build_verified`.
- **Mirror the ADR-0296 sidecar path with no marker** (the rejected fork of #1065). Smallest diff,
  no migration. Rejected: it lets an operator claim read as a KDIVE-verified capability, defeating
  the ADR-0286 invariant P5 is about — the reason Option B (this ADR) was chosen.
- **Per-operand attestation map** instead of a row-level boolean. More granular (mixed
  attested/verified operands in one row). Rejected as unneeded: an `s3` image has no build-verified
  operands to mix with, so a row-level marker is sufficient and simpler. Revisit if a source ever
  carries both.
- **A free-form operator provenance dict** rather than a typed `AttestedProvenance`. Rejected:
  `systems.toml` is validated operator config; a typed model catches a mistyped operand at load
  and bounds what an attestation can inject into the agent-facing `provenance`.
</content>
