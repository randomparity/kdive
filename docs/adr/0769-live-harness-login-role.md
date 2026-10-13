# 0769 — The live-tier harness login lives in the development bootstrap

## Status

Accepted

## Context

The live tiers (`live_stack` cells, the installed authority carriers, and their seeding helpers)
connect through `KDIVE_DATABASE_URL` and need two things. First, they read every public table
with a `project` column, including the three tables that migration 0104 revokes from `PUBLIC`
and every runtime capability. Second, they make four seeding writes. The only DSN that met both
needs was the migration owner's, and the owner can also run DDL (#3196). The grants have to live
somewhere: either in shipped schema or in the development-only role bootstrap that already
creates the fixed local login members.

## Decision

`deploy/compose/bootstrap-runtime-roles.sh` creates the `kdive-harness` login. It runs after
migrations and as the table owner, on every run of the role bootstrap. That run comes in each
services-stage bring-up, or explicitly after `just stack-backends`. On each run it converges the login's
grants:

- `SELECT` on every table in schema `public`
- `INSERT` and `UPDATE` on `budgets`, `quotas` and `resources`
- `INSERT` on `allocations`

It refuses a pre-existing role that is privileged, holds a membership, or owns anything. No
migration creates or grants the role. `scripts/live-stack/env.sh` exports its DSN as
`KDIVE_HARNESS_DATABASE_URL`. The operator chose this placement on 2026-10-13.

## Consequences

- Production, helm and externally provisioned (`KDIVE_LOCAL_ROLE_BOOTSTRAP=0`) databases have no
  harness role. A live tier run against them supplies its own least-privilege login.
- A table that a new migration adds becomes readable on the next role-bootstrap run, with no
  per-migration grant. Until then, the snapshot preflight fails, names the table, and names the
  bootstrap rerun as the fix.
- A refused pre-existing `kdive-harness` fails the role bootstrap, and with it the local
  bring-up.
- The grant set is checked by a `live_stack` test against a brought-up stack, not by the
  ordinary suite.

## Considered & rejected

- **Ship the role and grants in a new migration.** judgment: fit. It puts a test-only cluster
  role into every production database, against the issue's non-goal that the production role
  model stays unchanged. Its grants would also be a snapshot taken at migration time, so every
  later table would need its own grant or `ALTER DEFAULT PRIVILEGES`.
- **Keep the migration-owner DSN.** judgment: fit. The harness would keep DDL and ownership it
  never uses, and that over-grant is the defect #3196 records.
- **Enumerate the readable tables instead of granting all of `public`.** judgment: cost.
  `project_state` reads every table with a `project` column, discovered from `pg_catalog` at
  run time. A fixed list goes stale with each migration and fails the snapshot closed.
- **`ALTER DEFAULT PRIVILEGES` for the migration owner's new tables.** judgment: cost. It leaves
  a persistent ACL object outside the per-run revoke-and-grant convergence, which needs its own
  revocation on rollback. The rerun bootstrap already covers new tables.
