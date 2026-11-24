# Validate activation ownership before cleanup

## Problem

Production cleanup still has a successful path justified by legacy fixtures whose
artifact directories omit the configured System/Run/activation hierarchy. Removing
that return alone is insufficient: the supplied descriptor's device/inode check
currently runs during parent pruning, after payload deletion. Actual private-directory
probes delete five files for both a missing bound System and a wrong activation
descriptor; the latter raises only after deletion. The component already promises
that validation precedes its first unlink.

## Scope and design

Keep ownership in `LocalPayloadCleanup` and preserve its public callback signature.
Canonicalize the three binding components before path IO. Keep the existing recovery
root/archive validation and artifact-reference parsing in their current relative order.
Before projection opening or deletion, perform a read-only no-follow walk from the
configured root through System, Run and activation, checking existing private/euid
rules and supplied/resolved activation descriptor identity. Use stdlib `ExitStack`
for these bounded temporary descriptors and existing directory guards; create nothing.
New OSError paths use the existing fixed artifact refusal plus symbolic errno and
suppressed context, preserving path privacy. The already-open recovery descriptor
must close on refusal through the existing outer finally.

Retain the original identity check and bounded parent removal after cleanup. The
precheck does not replace the removal-time check. Remove the fixture-only missing
System success branch from that walk. Projection validation, archive cleanup, absent
owned-file convergence and nonempty-parent behavior remain unchanged. Host-only
cleanup remains valid while the recovered domain is active.

Migrate only fixtures that invoke real cleanup to the existing owned hierarchy.
Ordinary cleanup/retry helpers open through `LocalArtifactRoot`, recreating owned
parents after successful pruning. Explicit invalid-descriptor tests keep a separate
narrow injection helper so root-mode, missing-root and canonical-binding tests still
reach the real component. Fake-only artifact-root seams remain unchanged.
No public/session facade, power policy, schema, dependency or deployment change.

Existing [ADR-0602](../../adr/0602-local-external-boot-storage-is-reclaimed-by-owned-identities.md)
already requires exact owned cleanup and untouched ambiguous state. The remaining
root/privacy decisions in [ADR-0591](../../adr/0591-local-external-boot-session-mechanisms-bind-to-the-recovery-root.md)
still govern. This restores those contracts; no new ADR or accepted-body edit is needed.

## Failure model

- Actors/deployments: trusted local session callbacks and retries, with metadata and
  descriptors checked defensively on Linux x86_64 and ppc64le. Proof is local real
  filesystem plus fake provider boundaries, not guest or hardware qualification.
- Assets: payload/recovery bytes, sibling activations, exact descriptor ownership,
  private paths and finite descriptor lifetime.
- Accepted classes: no atomic protection against privileged concurrent rewriting of
  owned ancestor entries. The existing post-cleanup identity check remains. Supported
  operation retries acquire a fresh owned directory through the current factory;
  an arbitrary stale or foreign descriptor is not authority for deletion.
- Covered elsewhere: authenticated metadata, session binding/lease fences, guest-power
  policy, tombstone durability and hardware behavior retain their current owners.

## Success

1. Invalid ownership observed at entry rejects before any payload/archive mutation;
   missing bound directories, wrong descriptors and malformed components are covered.
2. New read-only walks use no-follow/private/euid checks, create no directories,
   release descriptors on success and failure, and retain fixed-message path privacy.
3. Original projection checks and post-cleanup identity/pruning remain effective;
   every existing partial-unlink/rmdir retry and sibling/nonempty-parent control passes.
4. Real-component fixtures use the owned hierarchy and retries reopen via its factory;
   live-domain host cleanup and real session metadata/descriptor gates remain covered.
5. Source changes stay in one existing component module and its existing test module;
   unchanged callers/SQL and all scoped guardrails/reviews are proven before commit.

## Validation

Turn the actual missing-bound/wrong-descriptor probes into regression tests asserting
unchanged payload and archive bytes, then observe old-source RED before implementation.
Use explicit negative descriptor injection, with canonical System/Run/activation cases
and real-source open failures at each hierarchy level. Track owned descriptors and
check they are closed; preserve the caller-owned descriptor. Controlled removal of
prevalidation and the post-prune identity comparison must be detected independently.
Run mechanism, session, external-boot and system-authority caller files; these include
active-domain cleanup, malformed projection, retry and tombstone paths. Freeze target
hashes, run lint/whole types/hooks and independent code/security reviews. No prose
snapshot, new model evaluation, broad filesystem framework or live claim is required.
Final campaign CI and whole-branch review remain separate obligations.
