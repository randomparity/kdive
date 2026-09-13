# Image-build worker image-catalog INSERT design

## Goal

Allow the existing `image_build` worker publish flow to create a new public image-catalog row
without broadening any other worker database authority.

## Design

Migration 0154 grants only `INSERT` on `public.image_catalog` to `kdive_worker`. The existing
UPDATE grant remains unchanged. The declared worker-write inventory adds the matching direct
`image-build.image-catalog.insert` entry, so ADR-0653's migrated-catalog guard proves the
effective grant after all migrations. The broader baseline records the migration as its grant
evidence, marks the write covered, and removes it from confirmed leaks.

The image-build handler test stages its job as the migration owner, then invokes the real publish
handler over a `kdive_worker` login. A registered row and stored object prove the worker-role path
runs through the INSERT successfully.

## Constraints

- Scope is migration 0154 and image-build worker-role evidence only.
- The grant is table-specific and operation-specific: no UPDATE, DELETE, SELECT, schema, or role
  membership changes are added.
- ADR-0653 remains the governing decision; no new architectural decision is introduced.

## Threat model

The widened boundary is a production database privilege used by the trusted worker process. The
worker process can now insert catalog rows, but only into `public.image_catalog`; PostgreSQL
enforces that restriction for every session authenticated through the worker role. Authenticated
tenants do not receive this database credential and continue to reach the handler through existing
server authorization. This change does not address compromised worker credentials or validation of
publish inputs; those are existing worker and publish-service controls.

## Acceptance evidence

- The migrated-catalog grant guard observes `has_table_privilege(kdive_worker,
  public.image_catalog, INSERT)`.
- The worker-role handler proof registers an image and stores its object.
- The baseline and inventory remain structurally valid and declare no image-catalog INSERT leak.
