# The authority teardown runs on a released or expired Allocation (#2992)

## Scope and authority

Campaign b511ca261dbb-594c490b-8a6a-49ec-ab7c-648742ee7a61, issue #2992, token
`q2992-26d61560`. The campaign assigned migration 0169 and no ADR number: the decision is a dated
amendment to [ADR-0584](../../adr/0584-provider-host-authority-fences-external-boot-mutations.md)
and [ADR-0620](../../adr/0620-authority-owned-system-teardown.md). Approved exclusions (operator,
2026-10-01): relaxing the #2966 `allocations.release` refusal; an `authority_system_v1` marker with
no activation; the 0149 provisioning fence (`register_authority_system_ownership`).

## Problem

Three security-definer functions refuse every purpose unless the System's Allocation is
`active`. Each has the literal `v_allocation.state <> 'active'` exactly once in its live
definition (`pg_get_functiondef` on a database migrated through 0168):

- `allocate_external_boot_authority` (0122:454);
- `acknowledge_external_boot_authority` (0122:651);
- `commit_external_boot_authority_result` (0122:905).

`finalize_external_boot_authority_teardown` has no Allocation-state fence. Lease expiry moves an
Allocation `active -> expired` while a System with external-boot history is not `torn_down`
(#2966 refuses only `allocations.release`). `systems.teardown` then routes the authority teardown,
the allocator answers `superseded`, and the System has no exit. Nothing else on the teardown route
reads the Allocation state: `route_external_boot_teardown` and `_teardown_prerequisites` (in
`src/kdive/mcp/tools/lifecycle/systems/admin.py` and
`src/kdive/jobs/handlers/external_boot/lifecycle.py`) do not.

## Design

1. Migration `0169_ended_allocation_authority_teardown.sql` patches the three functions in one
   `DO` block, the 0168 pattern. For each, it asserts that `v_allocation.state <> 'active'`
   occurs exactly once and that the replacement text is absent, else raises
   `external boot authority Allocation fence changed in <function>`. It replaces the literal with
   `(v_allocation.state <> 'active' AND p_purpose <> 'teardown')` and re-executes the definition;
   `CREATE OR REPLACE` keeps owner and grants. No other predicate changes.
2. `p_purpose` is safe to read in all three: each function rejects a NULL or unknown `p_purpose`
   before the fence, and the same `OR` chain binds it to the stored or marked purpose (allocator:
   marker `purpose` and job kind `teardown`; acknowledgement and commit:
   `v_authority.purpose <> p_purpose`). A non-teardown authority cannot pass the fence by
   claiming purpose `teardown`.
3. The relaxation covers every Allocation state, as the issue asks ("regardless of allocation
   state"); `released` and `expired` are the states the tests pin.
4. The reservation still releases at most once: the receipt keyed by `root_authority_id` is
   unchanged, and nothing on the Allocation release or expiry path touches
   `external_boot_reservations`.
5. ADR-0584 and ADR-0620 get dated amendments stating the rule. The runbook
   `docs/operating/runbooks/stuck-tearing-down-system.md` names `systems.teardown` as the exit
   after a lease expires.

## Failure model

1. Actors and deployments
   - A project admin (`systems.teardown`) or platform admin (`ops.force_teardown`) over MCP; the
     worker running the marked TEARDOWN job; the provider-host authority acknowledging it. Local
     and remote libvirt (ADR-0584).
2. Invariants and assets at stake
   - Purposes `activate`, `recover`, `resolve-conflict`, and `release` keep the `active` fence at
     all three functions.
   - The reservation release row is written at most once per activation.
   - Every other fence on the route is unchanged: credential, job attempt, generation, binding,
     System state, newest activation, acknowledgement.
3. Accepted failure classes
   - A teardown racing an in-flight Allocation release (`releasing`): no longer superseded. Both
     run under the Allocation and System advisory locks, and the provider-host authority still
     serializes the mutation; tearing down the System is what the release needs anyway.
   - The orphaned-System lane still enqueues an unmarked teardown for such a System; the worker
     refuses it and `systems.teardown` replaces it (ADR-0620 #2966 amendment). Not changed here.
4. Covered elsewhere
   - `allocations.release` refusal: operator (approved exclusion).
   - `authority_system_v1` teardown with no activation and the 0149 fence: operator.

## Success

- On a database migrated through 0169, for Allocation states `released` and `expired`:
  - teardown `allocate` returns `allocated`, the real `acknowledge` returns `applied`, and a
    `complete_ready` finalize returns `applied` twice with one release row and the System
    `torn_down`;
  - a teardown failure commit returns `('applied', 'queued')`.
- On an `expired` Allocation, each of `activate`, `recover`, `resolve-conflict`, `release`:
  `allocate` returns `superseded` and writes no authority row; `acknowledge` returns `superseded`
  and writes nothing; a `fail` commit returns `('authority_superseded', 'failed')` and leaves the
  activation and System state unchanged.
- The 0168 test that pinned `superseded` on an `expired` Allocation is removed; its inverse is in
  the 0169 suite.
- End to end, `systems.teardown` on a System whose Allocation is `expired` queues the authority
  teardown, one worker pass succeeds it, and the System and activation are `torn_down`.

## Validation

See the plan, `docs/workflow/plans/2026-10-01-ended-allocation-authority-teardown.md`.
