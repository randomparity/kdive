# Private-image deletion concealment

## Problem

`images.delete` returns configuration_error for an absent ID and authorization_denied
for an existing private image outside the caller's projects. The category distinguishes
hidden existence even though the non-member denial omits the role name.
Catalog visibility and ADR-0123/0490 protect resource-existence information.

## Scope and design

After resolving a private row and its owner, check membership in `ctx.projects`
before `require_role`. A foreign owner returns the existing `_config_error(image_id)`
used for an absent row. Keep public/ownerless rejection, invalid UUID handling,
member role checks and audited RoleDenied behavior. Remove the now-unreachable base
AuthorizationError catch/import; retain the shared nullable denial helper because
other callers use it. No new helper or service is needed for this condition.
Keep `_delete_owned` and its transaction, row lock, publication/live-reference guards,
audit and success/conflict responses unchanged. Add the concealment and operator
requirement to the decorated wrapper's docstring and regenerate its tool reference.
Arguments and response schemas remain unchanged; this corrects foreign error
classification rather than adding a wire contract. Existing absent category stays.

## Success

1. Foreign private and absent rows have equal complete response envelopes after only
   normalizing the caller-supplied object ID, with no project-role or existence detail.
2. Foreign rows survive and produce no project audit; project members without operator
   retain audited denial and the required role, including membership without a role.
3. Existing authorized deletion, public rejection, reference and publication guards pass.

## Failure model

- Actors/deployments: authenticated MCP callers, with project memberships and independent
  role grants, target private catalog IDs on the existing server.
- Invariants/assets: hidden image existence and project identity; operator-only deletion;
  authorized member denial audit; unchanged backing-object and transaction behavior.
- Accepted failures: database corruption/outages retain existing error handling; concurrent
  image mutation policy is unchanged, and no new mutation path is introduced.
- Covered elsewhere: verified RequestContext membership/RBAC, row mutation concurrency
  and artifact reclamation remain their existing owners.

## Threat model

- Existing boundary: caller-controlled image ID resolves a row whose private owner may
  be outside ctx.projects; output categories previously revealed existence. None added.
- Actor: a valid caller without membership, including a platform role without the image's
  project grant, must not acquire visibility or deletion authority from this endpoint.
- Control: map invisible private rows to the existing missing response before the role
  gate; retain member operator check/audit and existing typed envelope construction.
- Out of scope: upload, global pruning/extension, role-token validation, new timing
  guarantees and direct database administration. This adds no authority at those seams.

## Validation

Use real disposable Postgres in test_images_tools.py. Compare complete normalized
foreign/missing envelopes for cross-project, no-project and platform-admin callers;
assert row retention and absent project audit. Retain/member-test missing-role/viewer
and operator cases. Expected red: authorization_denied differs from configuration_error.
Remove the membership guard to verify the new test fails, then restore it. Run focused
image/catalog and denial-envelope guard tests, regenerate docs with the repository
recipe, lint/whole-tree type and hooks. Keep the implementation plan transient.
