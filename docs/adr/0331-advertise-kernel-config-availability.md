# ADR 0331 — Advertise kernel-config availability on images.list/describe

- **Status:** Accepted
- **Date:** 2026-07-12
- **Issue:** #1096 (`BLACK_BOX_REVIEW.md` F5, item 1)
- **Amends:** ADR-0317 (image kernel-config offer)
- **Builds on:** ADR-0311 (agent-facing image selection affordances)

## Context

ADR-0317 stores an image's extracted `/boot/config-<ver>` as a best-effort object-store
artifact, records its key on the nullable `image_catalog.kernel_config_key` column, and hands the
agent a presigned download URL through `images.kernel_config`. That ADR deliberately **withholds
`kernel_config_key` from the agent surface** ("like `object_key`"): the agent never sees the
object key.

The consequence, verified in `BLACK_BOX_REVIEW.md` F5: neither `images.list` nor `images.describe`
exposes *whether* an image offers a config at all. An agent that wants a known-good starting
`.config` can only discover availability by calling `images.kernel_config` and getting a
`kernel_config_unavailable` `configuration_error` back. On a catalog where many rows carry no
config — externally-baked (`s3`) images, operator-staged rows, pre-feature rows, and images whose
`/boot` lacked a single baseline kernel/config — that is a guaranteed dead call the agent cannot
avoid. The selection affordance is missing.

The two other F5 items — making the publish-time extraction failure loud rather than a silent
`None` (item 2), and aligning the served docs so the seed path is not promised for images that
cannot deliver it (item 3) — are already addressed on `main`. This ADR covers item 1 only.

## Decision

**Add a derived `has_kernel_config` boolean to `images.list` (each row) and `images.describe`,
computed as `kernel_config_key is not None`.**

`true` means the image advertises a downloadable `/boot/config-<ver>` starting point; `false`
means it has none and `images.kernel_config` would fail. The field sits alongside
`default_kernel_version` (ADR-0311/0317) as a plain build-fact selection affordance — an agent
compares images and decides whether to fetch a seed config in one `images.list` call, with no N+1
`images.describe` fan-out and no dead `images.kernel_config` probe.

**The internal `kernel_config_key` stays withheld.** We disclose only the *derived availability
bit*, not the object key. ADR-0317's rule that the key is agent-invisible (like `object_key`) is
preserved; this ADR amends that ADR only to add the honest one-bit disclosure the key's absence
otherwise hid.

### Why a plain boolean, not an ADR-0286 capability signal

The capability-signal framework (`capability_signals.py`) computes a feature answer from a
**build-recorded provenance operand** that may be absent in un-refreshed metadata, and degrades to
`unverified` so a stale record cannot report confident-but-wrong — that honesty invariant is the
framework's whole reason to exist. `has_kernel_config` has no such hazard: `kernel_config_key` is
a definitive row column, and ADR-0317's invariant holds that a registered row's key is set iff its
config object exists. Presence is a hard fact with no degradation state to model and no threshold
to compute, so forcing it through the signal framework would add ceremony (a provenance operand it
does not have, an `unverified` status that never applies) for no honesty gain. A plain derived
field is the correct, minimal shape — the same treatment `default_kernel_version` already gets.

## Consequences

- An agent reads `has_kernel_config` from `images.list`/`images.describe` and calls
  `images.kernel_config` only for a `true` row, avoiding the guaranteed-fail probe on every image
  that carries no config.
- The field is advisory at the object level: `true` reflects the persisted key, so the rare case
  of a key set but its object HEAD-missing at fetch time still degrades to
  `kernel_config_unavailable` in `images.kernel_config` (its own HEAD gate is unchanged). No reader
  should treat `true` as a fetch guarantee — it is a "config is on offer" selection signal.
- No schema or storage change: the field is derived at response-shaping time from the existing
  column. The generated tool reference and the served `toolsets-images` resource gain the field.

## Considered & rejected

- **Expose `kernel_config_key` directly.** Reverses ADR-0317's withhold decision and leaks an
  internal object key for no agent benefit; the agent needs availability, not the key. Rejected.
- **Model it as an ADR-0286 capability signal.** The framework exists for provenance operands that
  can go stale and lie; a definitive row column has no such failure mode. Rejected as premature
  abstraction (see above).
- **Leave discovery to the `images.kernel_config` failure.** That is exactly the F5 gap: a dead
  call the agent cannot avoid. Rejected.
