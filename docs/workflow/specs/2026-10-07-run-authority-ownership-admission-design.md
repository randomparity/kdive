# Run authority ownership admission

## Problem and evidence

Issue #3138 requests rejection before durable work when a provider authority route
is configured but the System belongs to a fixed worker. Current `steps.py:140,436`
selects only the provider route. `authority_owned.py:91` already exposes durable
System ownership through the server-authorized SQL reader. Existing install and
connected boot tests seed ordinary Systems and still accept authority routing;
three such cases reproduced at d7a0d6a3. The recovery/admission baseline passed
775 cases in 12.39 seconds; that does not validate the defective routing.

## Scope and architecture

The existing authority ownership service owns the new internal check:
`require_authority_system_binding(conn, system_id, authority_instance) -> None`.
It reads `authority_system_binding` and raises the existing categorized error type
if absent or if the stored authority instance differs. Absence uses reason
`authority_system_binding_missing`; mismatch uses `authority_system_binding_mismatch`.
No credential resolution or provider I/O occurs here.

Both public Run handlers invoke it only for a non-null route, after project/RBAC
checks and before `keyed_mutation`. Both existing System-locked enqueue paths
recheck before root reads, replay selection or mutations. Install uses its already
resolved immutable route; boot also retains its existing route-change check.
Categorized ownership-read failures render through `ToolResponse.failure_from_error`.

Use actionable detail: the configured external-boot authority does not own this
System; restore the route to its existing owner or provision a new System through
the intended authority. Do not suggest altering database ownership rows.

No authority route retains the ordinary path, including existing replay behavior.
Matching authority binding retains root validation, activation policy and force
boot behavior. This check does not replace those guards or add ownership-state
restrictions. Existing tests meant to exercise authority routing must seed real
authority ownership, rather than rely on the defect.

## Success

- Install and boot reject missing/mismatched ownership before keyed results,
  job enqueue/recycle, run-step deletion, activation or reservation creation.
- No-route and matching-owner paths retain their existing outcomes and replay.
- Authorization still precedes ownership diagnostics; foreign Runs disclose no owner.
- Agent-visible wrapper docstrings describe the refusal and operator action.
- Real DB tests cover invalid route, valid authority, ordinary path, keyed replay,
  existing installed variants and malformed reader results where reachable.
- An assigned x86 stack exercises public MCP refusal with candidate-matched roles;
  retain actual identities and no-mutation evidence. No POWER qualification claim.
- Record the verified limitation for already-stranded Systems and preserve issue
  #3138's recovery criterion until root can resolve it safely. No false completion.

## Recovery investigation

`runs.release_external_boot` accepts only active activations. Preparing activations
allow `systems.teardown`, but the request follows the original activate job's
authority marker (`test_external_boot_unrouted_teardown.py:70`). The local adapter
uses its fixed authority session (`external_boot.py:1889`), not the fixed worker's
daemon. An absence observation on that session does not establish absence on the
actual owner. `ops.force_release` still uses the guarded release service.

The smallest existing candidate is cancel the exact failed activation job and
request `systems.teardown`; this is not a verified remedy for cross-owner Systems.
The missing prerequisite is a supported, audited cleanup operation that can prove
both the erroneous authority activation is settled and the actual worker-owned
System is gone, without substituting one daemon's absence for the other's.
Do not execute this candidate on retained native POWER state or document it as
safe. Root must disposition the unresolved criterion under existing #3138; no new
issue or recovery interface is authorized in this task.

## Failure model

- Actors and deployments: authenticated project contributors on local/remote
  libvirt deployments; operators configure routes; server reads durable ownership.
- Invariants and assets: the actual System owner receives mutation; invalid routes
  cannot create activation fences or keyed mutation results; RBAC and replay remain.
- Accepted failure classes: an operator may change configuration between requests;
  current invalid configuration rejects replay until restored. Existing lifecycle
  guards retain ownership-state, teardown and run-binding race handling.
- Covered elsewhere: provider conflicts #3144; attempt recovery #3143; DWARF
  #3130/#3131; native POWER qualification #2818. Historical cross-owner recovery is
  unresolved here, not assigned to those other issues.

## Threat model

- Boundaries: tenant request to server authorization; configured provider route to
  durable System owner; server admission to privileged worker job. None is widened.
- Actors: project contributors cannot choose arbitrary authority identity; trust
  operator configuration and the existing restricted durable ownership reader.
- Controls: authorize Run before lookup; exact System UUID and authority instance;
  validate reader shape through existing service; recheck inside locked admission.
  Errors reveal only ownership incompatibility after project authorization.
- Out of scope: compromised database/admin, new secret transports and authority
  deployment policy. Existing provider deletion/retention contracts remain intact.

## Constraints

Python 3.14; existing dependencies only; Linux x86_64 and ppc64le targets remain.
No schema, migration, version or ADR index edits. Required ADR0752 is centrally
reserved. Full-spec M250; final iterating two passes. User approved the exact
scope exclusions on 2026-10-07; any additional recovery contract needs a checkpoint.
