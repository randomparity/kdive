# Live-tier harness login role: implementation plan (#3196)

**Goal:** the live-tier harness reaches Postgres through a least-privilege development login, not
the migration owner.

**Architecture:** `deploy/compose/bootstrap-runtime-roles.sh` creates the `kdive-harness` login
and converges its table grants on every bring-up. It runs after the migrations, as the table
owner. `scripts/live-stack/env.sh` exports that login's DSN as `KDIVE_HARNESS_DATABASE_URL`, and
every per-process DSN scrub drops it. The harness preflights and the runbooks name it. Spec:
`docs/workflow/specs/2026-10-13-live-harness-login-role-design.md`.

**Tech stack:** bash, PL/pgSQL (Postgres 17), Python 3.14, pytest, psycopg 3.

Expected implementation size: 170–240 changed lines (M) — summed from the file map. That is
about 45 bootstrap SQL lines, about 70 lines of new live test, about 25 scrub/env/registry
lines, about 30 message and unit-test lines, and about 40 runbook lines.

## Global Constraints

- No migration file and no change to `src/kdive/db/schema/`. Production, helm and externally
  provisioned members stay as they are (exclusions a and b).
- The script makes no `GRANT` or `REVOKE` for `PUBLIC`, `kdive_server`, `kdive_worker`,
  `kdive_reconciler`, `kdive_lifecycle_witness` or `kdive_provider_authority`.
- The harness login is named `kdive-harness`, with password `kdive-harness-local`. Every literal
  development credential line carries `# pragma: allowlist secret` in shell, or
  `-- pragma: allowlist secret` in SQL.
- The tool shell is zsh, so run multi-line verification under `bash`. Run gates bare. The full
  gate is `just ci > <file> 2>&1 < /dev/null`.
- Live compose checks run under
  `flock /home/dave/src/kdive/.agent/campaigns/73d4edacff-livestack.lock`, with
  `KDIVE_OIDC_PORT=18190` exported before sourcing `env.sh`. Remove any containers you create,
  and never wipe the existing volumes.

## File map

| File | Change |
|------|--------|
| `deploy/compose/bootstrap-runtime-roles.sh` | create or verify `kdive-harness`; converge its grants |
| `deploy/compose/README.md` | name the harness login among the local-only members |
| `tests/integration/live_stack/test_harness_role.py` (new) | live privilege proof |
| `tests/compose/test_compose_config.py` | the bootstrap names the harness login |
| `scripts/live-stack/env.sh` | export `KDIVE_HARNESS_DATABASE_URL` |
| `src/kdive/config/external_env.py`, `docs/guide/reference/config.md` | catalogue the variable; regenerate the reference |
| `scripts/live-stack/{lib,stack-services,onboard,stack-status,apply-migrations,worker-lifecycle}.sh`, `examples/local-libvirt/build-image.sh`, the `live-stack.md` `verify-profile-kinds` snippet | add `-u KDIVE_HARNESS_DATABASE_URL` to each scrub |
| `tests/scripts/test_live_stack_scripts.py` | export list and lifecycle scrub canary |
| `tests/integration/live_stack/{tool_cells,scenario}.py`, `test_snapshot_access.py` | name the harness DSN |
| `tests/live_vm/installed_local_authority_support.py`, `tests/live_vm/test_installed_local_authority_support.py` | name the harness DSN |
| `docs/operating/runbooks/live-testing.md`, `docs/operating/runbooks/live-stack.md` | document the harness DSN |

## Task 1: harness login and grants

**Interfaces:**
- Produces the database login `kdive-harness`, which Tasks 2 and 3 name.
- Consumes `tool_cells.require_snapshot_read_access(conn, tables)` and
  `installed_local_authority_support.require_evidence_read_access(db_url)`. Both exist on main
  with these signatures.

**Verification:**
- Contract: the bootstrap grants. Mode: `focused-test`.
  `tests/integration/live_stack/test_harness_role.py::test_harness_login_holds_exactly_the_harness_grants`.
  - Red: before step 3, connecting as the harness DSN fails with "password authentication
    failed" or "role does not exist".
  - Green: run the live command in step 4; it reports 1 passed.
- Contract: the bootstrap names the harness login. Mode: `focused-test`.
  `tests/compose/test_compose_config.py::test_clean_local_database_bootstraps_distinct_exact_login_members`.
  - Red: it fails on the added `"kdive-harness" in script`.
  - Green: `just test-verbose tests/compose/test_compose_config.py`.

Steps:

1. Write `tests/integration/live_stack/test_harness_role.py`. Mark it `pytest.mark.live_stack` and
   skip when `KDIVE_HARNESS_DATABASE_URL` is unset. Connect with `psycopg.AsyncConnection` and
   assert the following:
   1. Over `pg_tables WHERE schemaname='public'`, `has_table_privilege(..., 'SELECT')` holds for
      every table.
   2. The set of `(table, privilege)` for which `INSERT`, `UPDATE`, `DELETE` or `TRUNCATE` holds
      equals the four grants.
   3. `has_schema_privilege('public','CREATE')` is false.
   4. No `pg_auth_members` row has `member = 'kdive-harness'::regrole`, and no `pg_class` row is
      owned by it.
   5. For each role in `('kdive_server','kdive_worker','kdive_reconciler','kdive_lifecycle_witness')`
      and each 0104 table, `has_table_privilege(role, 'public.'||t, 'SELECT')` is false.
   6. `require_snapshot_read_access` passes over every table with a `project` column, and
      `require_evidence_read_access(dsn)` returns.
   7. Inside `conn.transaction()` with a forced rollback, the two `seed_metering` upserts for a
      `harness-role-probe` project execute.
2. Add `assert "kdive-harness" in script` to the compose bootstrap test. Run it and see red.
3. Append to the bootstrap heredoc, after the membership `\gexec` blocks:
   1. A `DO $harness$` block. It creates `"kdive-harness" LOGIN NOINHERIT NOSUPERUSER NOCREATEDB
      NOCREATEROLE NOREPLICATION NOBYPASSRLS` when absent. It raises when the role is
      non-login, superuser, createdb, createrole, replication or bypassrls, when it holds any
      `pg_auth_members` membership, or when it owns any `pg_class` or `pg_namespace` row.
   2. `ALTER ROLE` sets the password to the development value `kdive-harness-local`, carrying the SQL pragma.
   3. In `BEGIN … COMMIT`:
      - `REVOKE ALL ON ALL TABLES IN SCHEMA public FROM "kdive-harness"`
      - `GRANT USAGE ON SCHEMA public`
      - `GRANT SELECT ON ALL TABLES IN SCHEMA public`
      - `GRANT INSERT, UPDATE ON TABLE public.budgets, public.quotas, public.resources`
      - `GRANT INSERT ON TABLE public.allocations`

   Add one README sentence.
4. Under the flock, run this:
   ```
   bash -c 'export KDIVE_OIDC_PORT=18190; source scripts/live-stack/env.sh;
   scripts/live-stack/stack-services.sh --stage backends;
   env -u KDIVE_MIGRATION_DATABASE_URL docker compose run --rm --no-deps role-bootstrap'
   ```
   Then run
   `KDIVE_HARNESS_DATABASE_URL=… uv run python -m pytest tests/integration/live_stack/test_harness_role.py -m live_stack -q`.
   Expect `1 passed`. Rerun the bootstrap to prove it is idempotent.
5. Commit with `feat(compose): provision a least-privilege live harness login`.

## Task 2: export and scrub the DSN

**Interfaces:** produces the environment variable `KDIVE_HARNESS_DATABASE_URL`, which Task 3
names.

**Verification:**
- Contract: env.sh exports it. Mode: `focused-test`.
  `tests/scripts/test_live_stack_scripts.py::test_live_stack_env_exports_required_defaults` with
  the name added. Red until env.sh exports it.
- Contract: the lifecycle wrapper scrubs it. Mode: `focused-test`.
  - Add `'KDIVE_HARNESS_DATABASE_URL'` to the probe tuple in `_lifecycle_status`, with
    `"harness-canary"` in its environment.
  - Make the existing assertion over the probe expect `<missing>` for that line.
  - Red until `worker-lifecycle.sh` adds `-u`.
- Contract: the other scrub sites. Mode: `task-test-not-applicable`. They are one-flag edits to
  `env -u` lists on paths that start daemons or onboarding, and no unit test runs any of them.
  `just lint-shell` checks the syntax.
- Contract: the registry. Mode: `focused-test`. `just env-docs-check` and `just config-docs-check`
  fail until the entry exists and the reference is regenerated.

Steps:

1. Extend both tests and see red.
2. In env.sh, add `default_harness_login="kdive-harness:kdive-harness-local"` (pragma) and
   `export KDIVE_HARNESS_DATABASE_URL="${KDIVE_HARNESS_DATABASE_URL:-postgresql://${default_harness_login}@${database_host}}"`.
   Rewrite the comment so it says the harness login is the live tiers' DSN.
3. Add `-u KDIVE_HARNESS_DATABASE_URL` to every scrub site in the file map.
4. Add an `ExternalEnvVar("KDIVE_HARNESS_DATABASE_URL", "script", "local Compose harness DSN", …)`
   entry after the reconciler entry. Run `just config-docs`.
5. Run `just test-verbose tests/scripts/test_live_stack_scripts.py`, `just lint-shell`,
   `just env-docs-check` and `just config-docs-check`, and expect all green. Commit with
   `feat(live-stack): export the harness DSN and scrub it from daemons`.

## Task 3: preflights and runbooks name the harness DSN

**Verification:**
- Contract: the snapshot, scenario and carrier messages. Mode: `focused-test`.
  - Change `test_snapshot_access.py` to expect `KDIVE_HARNESS_DATABASE_URL` and to reject
    `KDIVE_MIGRATION_DATABASE_URL`.
  - In `test_installed_local_authority_support.py`, `test_evidence_read_preflight_names_the_private_table_and_required_role` expects
    `KDIVE_HARNESS_DATABASE_URL` in place of `migration-owner DSN`.
  - Red before the message edit. Green:
    `just test-verbose tests/integration/live_stack/test_snapshot_access.py tests/live_vm/test_installed_local_authority_support.py`.
- Contract: the runbooks. Mode: `task-test-not-applicable`. They are prose with no executable
  consumer. `just docs-check docs-links` covers their anchors.

Steps:

1. Edit the tests and see red.
2. Change the three messages to the form
   `KDIVE_DATABASE_URL="$KDIVE_HARNESS_DATABASE_URL" (the least-privilege harness login)`.
   Drop "the migration owner" from the comments at `tool_cells.py` (the read-only session
   comment) and `installed_local_authority_support.py:1920`.
3. Edit `live-testing.md` at the carrier DSN paragraph, the POWER carrier, the tool-cells
   paragraph (including dropping the "#3196 tracks" sentence) and the catalog and later carriers.
   Edit the `live-stack.md` DSN table, adding a harness row and narrowing the migration row to
   migration and bootstrap.
4. Run the focused tests, `just lint`, `just type` and `just docs-check`. Commit with
   `docs(live-testing): run the harness on the least-privilege login`.

## Rollback

Revert the commits. Without the bootstrap SQL, an existing dev database keeps the
`kdive-harness` role with its grants, and that is harmless. To remove it, run
`DROP OWNED BY "kdive-harness"; DROP ROLE "kdive-harness";` as the migration owner.
