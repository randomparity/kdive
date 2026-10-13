# Least-privilege live-tier harness login role (#3196)

## Problem

The live-tier harness connects to Postgres through `KDIVE_DATABASE_URL`. That covers the
`live_stack` cells, the installed authority carriers, and the seeding helpers they call. The
runbooks and the two read preflights tell operators to set it to the migration-owner DSN. The
migration owner owns every table and can run DDL, yet the harness only reads rows and makes four
seeding writes. The server DSN cannot be used instead, because migration 0104 revokes
`worker_incarnations`, `investigation_build_uses` and `investigation_build_use_recoveries` from
`PUBLIC` and from every runtime capability role. Both the snapshot and the carriers read those
tables.

The harness touches the database as follows:

| Caller | Statements |
|--------|------------|
| `tool_cells.project_state` | `SELECT` on every public table with a `project` column, found from `pg_catalog` at run time |
| carrier preflight `_CARRIER_READ_TABLES`, plus the carrier proofs | `SELECT` on `systems`, `jobs` and the `external_boot_*` evidence tables |
| `catalog.py`, `spine.py`, `secret_presence.py`, `remote_quiescence.py`, kdivectl boundary | `SELECT` on resources, allocations, shapes, ledger, audit and system tables |
| `spine.seed_metering` | `INSERT … ON CONFLICT DO UPDATE` on `budgets` and `quotas` |
| `_seed.seed_granted_allocation` (kdivectl boundary) | `SELECT … FOR UPDATE`, `INSERT` and `UPDATE` on `resources`; `INSERT … RETURNING` on `allocations` |

## Decision

[ADR-0769](../../adr/0769-live-harness-login-role.md) records it. The grants live only in the
development bootstrap `deploy/compose/bootstrap-runtime-roles.sh`, with no migration. The
operator chose this on 2026-10-13.

## Design

1. **Role.** The bootstrap adds the login `kdive-harness` (password `kdive-harness-local`) with the
   members' attribute check, except that it is `NOINHERIT`: `LOGIN`, no superuser, no createdb, no
   createrole, no replication, no bypassrls. It must also be a member of no role (member side
   only: the PG16+ ADMIN grant its creator holds runs the other way). It must own no relation
   or schema, and the bootstrap user must hold ADMIN on it. Otherwise the script raises an
   error that names the fix instead of converging. A
   bootstrap that drops ownership would be a destructive surprise. A role that owns or inherits
   more than its grants is not least privilege.
2. **Grants.** These converge in one transaction on every run of the role bootstrap. That run
   happens on every services-stage bring-up (`stack-services.sh`, `demo-up.sh`), and
   `just stack-backends` is followed by it explicitly, as is already the case for the runtime
   members. The bootstrap runs as the table owner, after migrations:
   - `REVOKE ALL ON ALL TABLES IN SCHEMA public FROM "kdive-harness"`
   - `GRANT USAGE ON SCHEMA public`
   - `GRANT SELECT ON ALL TABLES IN SCHEMA public`, which covers tables a later migration adds
     on the next bring-up
   - `GRANT INSERT, UPDATE ON budgets, quotas, resources`
   - `GRANT INSERT ON allocations`

   The script grants nothing to `PUBLIC` or to any capability role, and it does not touch the
   0104 revocations.
3. **DSN.** `scripts/live-stack/env.sh` exports `KDIVE_HARNESS_DATABASE_URL`, with
   the `kdive-harness` login at the env.sh database host as the default. It is catalogued
   in `src/kdive/config/external_env.py` with scope `script`, and the config reference is
   regenerated. Every existing `env -u` DSN scrub also drops it: `lib.sh`, `stack-services.sh`,
   `onboard.sh`, `stack-status.sh`, `apply-migrations.sh`, `worker-lifecycle.sh` and
   `examples/local-libvirt/build-image.sh`, plus the `verify-profile-kinds` snippet in `live-stack.md`. That keeps #1929's rule that no process receives
   another authority's DSN.
4. **Messages and runbooks.** The snapshot preflight (`tool_cells.py`), the
   `scenario.prerequisites` blocked reason and the carrier preflight
   (`installed_local_authority_support.py`) name `KDIVE_DATABASE_URL="$KDIVE_HARNESS_DATABASE_URL"`.
   `live-testing.md`, the `live-stack.md` DSN table, and the current prerequisite in
   `remote-live-stack.md` do the same. The messages also name the bootstrap rerun for a DSN
   that is already set. `deploy/compose/README.md`
   lists the harness login among the local-only members.

## Failure model

1. **Actors and deployments.**
   - A local operator or CI job runs the live tiers against the compose backends that
     `stack-services.sh` brings up, on a developer host or a lab guest.
   - Deployments with `KDIVE_LOCAL_ROLE_BOOTSTRAP=0`, helm, and production get no harness role.
2. **Invariants and assets.**
   - The 0104 revocations stay as they are for `PUBLIC` and every runtime capability role.
   - No runtime role gains a grant.
   - The harness role holds no DDL, no ownership, no membership, and no write beyond the four
     tables above.
3. **Accepted failure classes.**
   - The harness reads every public table, including any secret-bearing columns. It is accepted
     because the role exists only in the local dev database, behind a published dev password,
     next to a migration-owner DSN that already reads everything.
   - A table added by a migration is unreadable to the harness until the role bootstrap runs
     again. `--stage backends` and `just migrate` do not run it. The snapshot preflight then
     fails closed, names the table, and names the bootstrap rerun as the fix.
   - A harness refusal fails the whole role-bootstrap one-shot, and with it the local
     bring-up. That is accepted because the refusal fires only on a foreign or privileged
     `kdive-harness`, which needs an operator anyway.
4. **Covered elsewhere.**
   - Externally provisioned databases supply their own members. That is the existing
     `KDIVE_LOCAL_ROLE_BOOTSTRAP=0` contract.
   - `.github/workflows/live.yml` lanes set `KDIVE_DATABASE_URL` to the server DSN and run no
     snapshot cells. They are unchanged.

### Threat model

- **Boundaries.**
  - Added: a new login authenticating to the dev database.
  - Widened: none. The script edits no existing role's grants.
- **Actors.** The local operator and the CI job. Trust stays on the host that holds the
  development credentials. Those credentials are already published in `env.sh` for every member.
- **Controls.**
  - Postgres privilege checks.
  - The bootstrap's attribute, membership and ownership assertions.
  - The read preflights, which fail before any mutation.
- **Out of scope.** Network exposure of the dev Postgres port. That is governed by compose's
  publish mapping and is unchanged.

## Validation

- **Bootstrap grants** (focused-test, live): `tests/integration/live_stack/test_harness_role.py`
  is marked `live_stack` and skips without `KDIVE_HARNESS_DATABASE_URL`. Connected as the harness
  DSN, it asserts the following:
  - `has_table_privilege` gives `SELECT` on every public table.
  - The write privileges are exactly the four grants above.
  - `CREATE` on the schema is denied.
  - The role holds no membership and owns nothing.
  - `kdive_server`, `kdive_worker`, `kdive_reconciler` and `kdive_lifecycle_witness` still lack
    `SELECT` on the three 0104 tables, and no ACL entry on them grants to `PUBLIC`.
  - Both read preflights pass, and `seed_metering` succeeds against its fixed probe project.
  - The `seed_granted_allocation` calls (`register_discovered_resource` and
    `ALLOCATIONS.insert`) succeed inside a transaction that is then rolled back.

  It is red before the bootstrap change, because the login fails or the privileges are missing.
- **Preflight messages** (focused-test): `test_snapshot_access.py` and
  `test_installed_local_authority_support.py` assert that the messages name
  `KDIVE_HARNESS_DATABASE_URL` and no longer name the migration owner.
- **Bootstrap text** (focused-test): `tests/compose/test_compose_config.py` asserts that the
  script provisions `kdive-harness`.
- **env.sh export and scrubs** (focused-test): `tests/scripts/test_live_stack_scripts.py`
  asserts that `env.sh` exports the harness DSN. Its lifecycle-wrapper canary asserts that
  `worker-lifecycle.sh` drops it. The other scrub sites are one-flag edits that match their
  existing `-u` lists (task-test-not-applicable: no test harness runs those daemons).
- **Runbooks** (task-test-not-applicable): these are prose that no executable consumer parses.
  `just docs-check` and `docs-links` cover their links.
