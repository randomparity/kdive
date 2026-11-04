# Pin the authority System SSH forward port once set (#3546)

## Scope and authority

Campaign scope for issue #3546, token `q3546-f1956dc7`. The operator approved on 2026-11-04 the
exclusions: NULL-to-value and same-value writes stay allowed; no audit of other columns; no
in-place edit of 0149 or 0189. Migration number 0191 was assigned by the campaign.

## Problem

Migration 0189 added `authority_system_ownership.ssh_forward_port`, the loopback endpoint the
server trusts for debug attach (ADR-0811). The ownership row's immutability trigger,
`reject_authority_system_binding_update` (defined only in 0149), does not cover the column, so a
later write could redirect that endpoint.

## Design

Migration `0191_authority_system_ssh_forward_immutable.sql` runs `CREATE OR REPLACE FUNCTION` on
the trigger function, with the 0149 body unchanged plus one clause beside the bootstrap and
first-activation clauses:

    IF OLD.ssh_forward_port IS NOT NULL
       AND NEW.ssh_forward_port IS DISTINCT FROM OLD.ssh_forward_port THEN
        RAISE EXCEPTION 'authority System SSH forward port is immutable';
    END IF;

`IS DISTINCT FROM` refuses value-to-NULL as well as value-to-other-value. `CREATE OR REPLACE`
keeps the 0149 trigger binding and the function's revoked privileges. A whole-body replacement
is used instead of the `pg_get_functiondef` patch idiom because the body is short and has one
prior definition. No caller changes; the trigger stays the single owner of the rule.

### Why the legitimate writers still pass

The finalizer and the reconciler repair are the only statements that write the column, and each
runs only on the transition from ownership `provisioning` to `ready`. No migration moves a row
back to `provisioning`, so each row takes that write at most once, from NULL. A receipt with no
usable port writes NULL over NULL, which the clause ignores.

## Failure model

1. Actors and deployments: the worker finalizer and reconciler repair, as SECURITY DEFINER
   functions; every deployment that applies the migrations.
2. Invariants and assets at stake: a non-NULL port never changes for the life of the row; the
   finalizer and repair keep committing, because a raised exception would abort the ready
   transition.
3. Accepted failure classes:
   - A NULL column may still be set outside finalize or repair. Approved exclusion, and only
     SECURITY DEFINER functions can reach the table (0149 revokes direct access).
   - A database owner can disable the trigger or delete the row. Same exposure as the other
     immutable fields; the trigger guards application paths.
   - Rows whose port was already rewritten are not detected. No writer other than the two above
     exists, so no such row is expected, and the column has no history to compare against.
4. Covered elsewhere: the audit of other columns stays with the operator.

## Threat model

- Boundary inventory: none added or widened. The existing boundary is the ownership row read by
  `resolve_authority_system_control_binding`; the receipt input is validated by the 0189 helper.
- Actor model: a defect or later code path in a process role reaching a SECURITY DEFINER writer.
  Process roles have no direct table privilege.
- Control: the BEFORE UPDATE trigger, which fires for every role including definers. A refusal
  raises the fixed message and leaks nothing else.
- Out of scope: the receipt's port value itself (signed by the authority, ADR-0811); database
  owner access.

## Validation

Tests in `tests/db/test_migration_0191_authority_system_ssh_forward_immutable.py`: value-to-other
and value-to-NULL raise; same-value rewrite and a state-only update pass; NULL-to-value passes.
The existing 0189 tests prove the finalizer and repair writes still commit. The test is shown red
with the clause removed.
