# Settled teardown routing — implementation plan

Issue #3551. Spec: [settled teardown routing](../specs/2026-11-11-settled-teardown-routing-design.md).
Branch `feat/settled-teardown-routing-3551`, base `main`. Guardrails: `just lint`, `just type`,
`just test-changed`, `prek run`, then `just ci`.

## Task 1 — settled predicates

`ExternalBootActivationRepository.settled_at(conn, activation_id)`, and
`settled_system_awaiting_teardown(conn, allocation_id)` in `services/external_boot/settlement.py`.

Verify: DB tests under each reading role (server, worker, reconciler) for settled, unsettled and
missing rows; the allocation query ignores a `torn_down` System.

## Task 2 — worker handler

`jobs/handlers/systems.py`: raise the #2966 refusal only when the latest activation is not settled.

Verify: an unsettled activation still refuses with `external_boot_teardown_not_supported`; a settled
latest activation reaches the provider teardown and `torn_down`; a newer unsettled activation after
a settled one refuses.

## Task 3 — routing

`mcp/tools/lifecycle/systems/admin.py`: `_route_settled_teardown`, called from
`route_external_boot_teardown` before `_enqueue_authority_teardown`.

Verify: `systems.teardown` and `ops.force_teardown` on a settled System enqueue an ordinary
`TeardownPayload` without consulting the binding; a failed authority-marked prior row is replaced;
a running one, or a claimed, canceled one written after `settled_at`, is replayed, and a claimed,
canceled one written before it is replaced; unsettled histories keep the authority route.

## Task 3b — stalled teardown repair

`reconciler/repairs/systems.py`: the stalled `tearing_down` repair skips its external-boot warning
for a settled latest activation and requeues.

Verify: a settled `tearing_down` System with a failed ordinary row is requeued; an unsettled one
still warns.

## Task 4 — lease expiry

`reconciler/repairs/allocations.py`: `_expire_one` returns `False` with a warning while
`settled_system_awaiting_teardown` names a System.

Verify: an elapsed allocation with a settled `ready` System stays `active`; once the System is
`torn_down` it expires.

## Task 5 — the tool

`mcp/tools/ops/security/breakglass.py`: `recover_stranded_activation` and its registration. The
docstring names the upgrade order and that the tool is not used until every server is upgraded.
`mcp/exposure.py`, `_docmeta.py` and the tool-doc and docmeta tests as their gates require.

Verify: denial audited and `denied`; observe audited and enqueues; settle refusals
(`observation_missing`, `observation_pending`, a failed observation's own reason, a primitive
refusal) each audited and returned unchanged; settle on a succeeded observation reaches the
primitive.

## Task 6 — docs and generated surfaces

Update the `ops.force_teardown` docstring, run `just docs`, `just cli-verbs`, `just rbac-matrix`,
`just resources-docs` as their checks demand; ops toolset page; coverage obligations under #3552.

Verify: each `*-check` recipe passes.
