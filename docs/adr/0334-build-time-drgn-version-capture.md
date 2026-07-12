# ADR 0334 — Capture the guest drgn version at build time into published-image provenance

- **Status:** Accepted
- **Date:** 2026-07-12
- **Issue:** #1127
- **Builds on:** ADR-0328 (live-drgn capability signal), ADR-0253 (kdump capability predicate / makedumpfile marker-probe pipeline), ADR-0323 (operator-attested provenance)

## Context

ADR-0328 promoted `live_drgn` to a registered capability signal computed from a per-image
`provenance["drgn_version"]` operand, and surfaced it through `images.describe`. For the curated
`rootfs_catalog.toml` rows that operand is a test-guarded snapshot, so the signal is authoritative
there. But ADR-0328 explicitly deferred wiring build-time capture ("Not in this change"): no
producer writes `provenance["drgn_version"]` for a **KDIVE-built** image, so `live_drgn` honestly
degrades to `unverified` for an agent that builds its own rootfs — identical to how
`makedumpfile_version` behaved for an externally-baked image before its marker/probe pipeline
existed. This closes that gap (BBR F1 recommendation 3) by giving the build planes the same
marker/probe capture `makedumpfile_version` already uses.

## Decision

Replicate the `makedumpfile_version` marker-probe pipeline for drgn.

1. **Marker writer.** A family-neutral `drgn_version_marker_args()` (in `_fedora_customize.py`,
   alongside `makedumpfile_version_marker_args()`) emits a best-effort virt-customize fragment that
   records `drgn --version` into the in-guest marker `DRGN_MARKER_GUEST_PATH`
   (`/usr/lib/kdive/drgn-version`). The debian and rhel/fedora customizers invoke it **only when the
   drgn/introspection package is installed** (`python3-drgn` on debian, `drgn` on rhel/fedora), so a
   build-host image with no drgn writes no marker. The drgn CLI is at `/usr/bin/drgn` on every debug
   image, so the writer is family-neutral.

2. **Read-only probe.** A `probe_drgn_marker` seam in `provenance_probes.py` (paralleling
   `probe_makedumpfile_marker`) reads the marker back with a read-only `guestfish cat`. It is
   advisory: it returns `None` for an absent/empty marker and raises only `MISSING_DEPENDENCY` /
   `INFRASTRUCTURE_FAILURE` (guestfish absent / timeout), which the build catches and degrades.

3. **Provenance capture.** `RootfsBuildProvenance` gains an optional `drgn_version: str | None`
   threaded through `local_libvirt(...)` and emitted via `_put_if_present(record,
   PROVENANCE_DRGN_VERSION, ...)`. `LocalLibvirtRootfsBuildPlane._capture_drgn` mirrors
   `_capture_makedumpfile`: the marker probe first, then a package-version fallback (`drgn` /
   `python3-drgn` from the inspected map), each parsed to a canonical `DrgnVersion`. Any failure,
   unparseable, or absent source degrades to `None` so the build still publishes.

A KDIVE-built debug image now carries `provenance["drgn_version"]`, and `images.describe` computes a
real `capable`/`incapable` `live_drgn` verdict (with the ADR-0323 `basis`) instead of `unverified`.

## Consequences

- The read path (ADR-0328 predicate + signal) is unchanged; it simply now receives an operand for
  KDIVE-built images. The honesty invariant holds: an image without the marker (a non-debug build,
  or one whose drgn was off `PATH`) still reports `unverified`, never a confident-but-wrong answer.
- No DB schema change: this is a provenance-dict field and an in-guest marker file.
- `remote-libvirt` provenance does not set `drgn_version` (its optional field defaults to omitted);
  wiring the remote plane's capture is out of scope.

## Alternatives considered

- **Leave `live_drgn` `unverified` for built images and rely only on the curated snapshot.** Leaves
  the F1 gap open for the agent-built-rootfs path the signal most needs to serve.
- **Read the drgn version only from the inspected package map, no in-guest marker.** The marker is
  authoritative (the binary's own `--version`) and matches the makedumpfile precedent exactly; the
  package map is retained only as the fallback.
- **Fail the build when drgn is present but the marker is empty.** Rejected: capture is advisory
  like every other provenance probe — a probe hiccup must never fail an otherwise-good build; the
  signal degrading to `unverified` is the correct, honest outcome.
