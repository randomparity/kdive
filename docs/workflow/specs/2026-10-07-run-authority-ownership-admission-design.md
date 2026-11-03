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

**Inventory field.** `LocalLibvirtInstance.relocated_authority_instance: str | None`
(`inventory/model.py`), `min_length=1`, `max_length=255`, non-blank, same validator as
`authority_instance`. A model validator rejects an entry that sets both
(`"authority_instance and relocated_authority_instance are mutually exclusive"`), so there is
no precedence rule to document. The remote provider is out of scope (no fixture lane there).
Accessor `local_relocated_authority_for_resource(name)` beside
`local_authority_instance_for_resource` in `providers/local_libvirt/config.py` (same
fail-closed handling of an invalid inventory). `providers/system_authority/routing.py` gains
`authority_admission_routes_for_resource(kind, name) -> frozenset[str]` returning the
Resource's route and/or relocation (empty for other kinds); the registrar keeps using
`authority_instance_for_resource` unchanged, so provisioning behaviour is untouched.

**Admission.** `admit_authority_route(conn, system_id, binding) -> str | None` in a new
`services/external_boot/route_admission.py` (not `routing.py`: worker handlers import that
module, and the import-closure test forbids `providers.local_libvirt.config` there):

1. `A = server_authority_instance(binding)`; `None` returns `None` (ordinary path).
2. `owner = authority_system_binding(conn, system_id)`; a binding with a different authority
   raises `authority_system_binding_mismatch`; a binding equal to `A` admits.
3. Otherwise `A` must be in the Resource's admission routes (none when the name is absent), else
   raise `authority_resource_route_mismatch`.
4. Return `A`.

Call sites: `install_run` and `boot_run` before `keyed_mutation`, and
`_restage_and_enqueue_install` / `_enqueue_external_boot` again under the System lock. Errors
render through `ToolResponse.failure_from_error`; the detail names the cause and the fix and
does not suggest editing database rows, and says an already fixed-worker-provisioned System is ended and re-provisioned on a routed Resource rather than fixed by inventory edits. Wrapper docstrings and the generated tool reference
state the refusal.

**Lane and docs.** The fixture-mode inventory copy (runbook "Fixture-mode inventory") replaces
the `[[local_libvirt]]` entry's `authority_instance` line with
`relocated_authority_instance = "<I>"` instead of deleting it; the runbook, the example inventory
and the `NO_PLAIN_BLOCKED`/`ROUTED_BLOCKED`/`FIXTURE_PROVENANCE` wording in
`tests/integration/live_stack/authority_lane.py` (#3144/#3148 are closed) are updated.
`provision-authority-fixture.py` and `installed_local_authority_support.py` are unchanged. The
lane's existing "no `authority_instance` on the allocated resource" check still holds.

No migration and no ADR-0623 change: the declaration is not an ownership row and grants no
ownership state; it only widens admission for the operator-named authority.

## Tests

Real-DB: defect case (no declaration) refused for install and boot incl. replayed key, no job
or activation; Resource route admitted; relocation declaration admitted (fixture); binding
equal admitted; binding for another authority refused even with a declaration; no server route
unchanged; ADR-0713 reuse test admitted once its Resource carries a matching route; stale
Run/System binding. Inventory: field parses, blank rejected, both-set rejected, absent default.
Import-closure test stays green.

## Success

- Defect case refused before keyed replay, enqueue, run-step deletion, activation or
  reservation; a replayed key is refused too.
- Declared fixture, authority-owned and no-route Systems keep their outcomes and replay.
- Authorization still precedes the diagnostics.

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
- Invariants: a Run reaches an authority only when the operator's inventory routes or declares the System's Resource for it, or a recorded binding names it (a declaration is Resource-wide trust, not per-System proof); refusals
  create no durable state; RBAC and replay remain.
- Accepted failure classes: a route or relocation declaration added to a Resource after the fixed worker provisioned Systems on it admits those Systems (no per-System evidence exists; ADR-0752 Consequences); an operator changing configuration between requests makes a
  replayed key refuse until the route is restored.
- Covered elsewhere: provider conflict #3144 (closed), attempt recovery #3143, DWARF
  staging #3130/#3131, stranded-allocation recovery (new issue).

## Threat model

Boundaries: tenant request to server authorization; configured route to Resource route;
server admission to privileged worker job. None is widened. Contributors cannot choose an
authority identity; both routes are operator configuration. The check runs after project
authorization, and errors disclose only the incompatibility, never a foreign owner.
