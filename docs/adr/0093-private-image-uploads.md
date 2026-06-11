# ADR 0093 — Private image uploads: owner-scoped, TTL'd, reconciler-pruned (M2.4)

- **Status:** Proposed
- **Date:** 2026-06-10
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0092](0092-image-rootfs-lifecycle.md) (the
  `image_catalog` table and publish/register path this scopes by owner),
  [ADR-0048](0048-external-build-artifact-ingestion.md) (the presigned-PUT ingest, size cap,
  and quarantine the upload reuses), [ADR-0021](0021-reconciler-loop-drift-repair.md) (the
  drift-repair loop the prune sweep extends), and [ADR-0006](0006-oidc-rbac-attribution.md)
  (the owner principal and `(principal, operator-cli)` audit attribution).
- **Spec:** [`../superpowers/specs/2026-06-10-m24-image-rootfs-lifecycle-design.md`](../superpowers/specs/2026-06-10-m24-image-rootfs-lifecycle-design.md)
- **Milestone:** M2.4

## Context

ADR-0092 manages operator-published **public** base images. Authors also need to test against
their own image — a one-off rootfs or a modified base — without publishing it platform-wide or
asking an operator to register it. Such an image must be visible only to its uploader, and it
must not accumulate forever: a per-user scratch image with no lifetime becomes unbounded
object-store cost and an operator cleanup chore.

The platform already has the primitives. External-build ingestion (ADR-0048) is exactly an
owner-scoped, validated, quarantined user upload. The reconciler already auto-reaps on a
Postgres `now()` predicate (expired allocations, idempotency GC). The catalog already filters
on a `visibility` seam.

## Decision

We will add a **private** image lifecycle to `image_catalog`, scoped to the uploader and
pruned by the reconciler on a lifetime.

1. **Owner scoping on the existing visibility seam.** A private row carries
   `visibility='private'`, `owner=<uploading principal>`, and a required `expires_at`. DB
   `CHECK` constraints tie `owner` and `expires_at` to `private`. Resolution becomes
   `visibility='public' OR (visibility='private' AND owner=:requester)` — one authz predicate,
   not a new mechanism. A private image is never visible or usable to a non-owner.
2. **Upload reuses the ADR-0048 ingest.** A project member uploads via presigned PUT; the
   artifact is size-capped, quarantined, and validated against the provider contract (guest
   agent, kdump, drgn, allowlisted helpers) before a private row is registered.
3. **The reconciler auto-prunes on expiry.** A `expired_private_images` sweep deletes the
   object and row of any `private` image with `expires_at < now()`, audited under
   `system:reconciler` — the same self-healing TTL the platform uses everywhere else. Operators
   can prune-early or extend a lifetime; users can delete their own at any time. There is no
   standing operator cleanup chore.

## Consequences

- `image_catalog` gains `owner`, `visibility`, and `expires_at` columns with `CHECK`
  constraints binding them to the private case (defined in the ADR-0092 migration `0023`).
- The reconciler gains a third sweep and a `ReconcileReport` count.
- `kdivectl images` gains `upload` / `delete` (owner) and `prune` / `extend` (operator) verbs;
  a cross-owner or unprivileged invocation is denied and audited.
- The default lifetime and the maximum extendable lifetime become `KDIVE_*` config values with
  generated-reference entries.

## Alternatives considered

- **A separate `private_images` table.** Cleaner row-type separation, but resolution would
  union two tables and the reconciler would sweep two backings; the visibility column already
  expresses the distinction in one table. Rejected for the single-table model.
- **No TTL; operator deletes private images manually.** Matches a literal reading of "operator
  pruning", but it is the only non-self-healing TTL on the platform and lets private storage
  grow until someone runs the chore. Rejected for reconciler auto-prune with operator
  override.
- **Expose private upload as an agent-facing MCP tool.** An author is often an agent, so this
  is tempting, but image management is an operator/author surface on the CLI/HTTP boundary and
  the band's non-goal keeps the agent MCP surface unchanged. Rejected; the upload stays on the
  CLI/HTTP boundary.
