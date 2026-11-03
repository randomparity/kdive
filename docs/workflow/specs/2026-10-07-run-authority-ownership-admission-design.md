# Run authority route admission

Campaign 40ad57a95e26-bd4494f6-1948-48ac-a482-b759df3b87af, issue #3138, token
`q3138-87cfdeba`. Decision: [ADR-0752](../../adr/0752-admit-authority-run-routing-by-resource-route.md).
Full-spec, complexity M (250 changed lines), review depth iterating.

## Problem and evidence

`server_authority_instance(binding)` (`services/external_boot/routing.py`) returns the
server-wide authority route. `install_run` and `boot_run` (`mcp/tools/lifecycle/runs/steps.py`)
route to the authority on that alone. A fixed-worker System on a Resource with no
`authority_instance` therefore reaches an authority that cannot find its domain; the
activation stays `preparing` and release is fenced (`external_boot_restricted`).

## Design

`admit_authority_route(conn, system_id, binding) -> str | None` in
`services/external_boot/routing.py` replaces direct `server_authority_instance` use in the
Run handlers:

1. `A = server_authority_instance(binding)`; `None` returns `None` (ordinary path).
2. Resource route = `authority_instance_for_resource(binding.kind, binding.resource_name)`
   (`None` when the name is absent). Unequal to `A` raises `configuration_error`
   `authority_resource_route_mismatch`.
3. If `authority_system_binding(conn, system_id)` exists with a different
   `authority_instance`, raise `authority_system_binding_mismatch`.
4. Return `A`.

Call sites: `install_run` and `boot_run` before `keyed_mutation` (so a stored key cannot
bypass it), and `_restage_and_enqueue_install` / `_enqueue_external_boot` again under the
System lock before any write. Raised errors render through
`ToolResponse.failure_from_error`. Detail text names the cause and the fix; it does not
suggest editing database rows. Wrapper docstrings and the generated tool reference state
the refusal.

No schema, migration, deployment setting or provider operation changes. Fixture-mode
relocated Systems on a routed Resource, authority-owned Systems and no-route Systems are
admitted as before; the live carriers are unchanged.

## Success

- Defect case refused before keyed replay, enqueue, run-step deletion, activation or
  reservation; a replayed key is refused too.
- Matching-route and no-route paths keep their outcomes and replay.
- Authorization still precedes the diagnostics.
- Real-DB tests cover: no Resource route, different Resource route, different recorded
  owner, owner matching, no-route, replay, stale Run/System binding.

## Stranded-allocation recovery (verified: not cleared)

`ops.force_teardown` takes the same authority-marked teardown route as `systems.teardown`
(`route_external_boot_teardown`); the live evidence in the issue shows that route failing
(`journal-conflict`, `provider-not-configured`) because the authority cannot observe a
fixed-worker domain. `ops.force_release` uses the guarded release service, which refuses
while the activation is open. Recovery is therefore split into its own issue; this change
designs no recovery contract.

## Failure model

- Actors and deployments: authenticated project contributors on local/remote libvirt
  deployments; operators configure routes and Resource `authority_instance`.
- Invariants: a Run reaches an authority only when the System's Resource names it; refusals
  create no durable state; RBAC and replay remain.
- Accepted failure classes: an operator changing configuration between requests makes a
  replayed key refuse until the route is restored.
- Covered elsewhere: provider conflict #3144 (closed), attempt recovery #3143, DWARF
  staging #3130/#3131, stranded-allocation recovery (new issue).

## Threat model

Boundaries: tenant request to server authorization; configured route to Resource route;
server admission to privileged worker job. None is widened. Contributors cannot choose an
authority identity; both routes are operator configuration. The check runs after project
authorization, and errors disclose only the incompatibility, never a foreign owner.
