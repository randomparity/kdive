#!/usr/bin/env bash
set -euo pipefail

if [[ "${KDIVE_LOCAL_ROLE_BOOTSTRAP:-}" == "0" ]]; then
  echo "local runtime-role bootstrap disabled; using externally provisioned login members"
  exit 0
fi
if [[ "${KDIVE_LOCAL_ROLE_BOOTSTRAP:-}" != "1" ]]; then
  echo "local runtime-role bootstrap requires KDIVE_LOCAL_ROLE_BOOTSTRAP=1" >&2
  exit 1
fi
: "${KDIVE_MIGRATION_DATABASE_URL:?set the local migration-owner DSN}"

psql "$KDIVE_MIGRATION_DATABASE_URL" --set ON_ERROR_STOP=1 <<'SQL'
DO $bootstrap$
DECLARE
    member_name text;
    attributes_match boolean;
BEGIN
    FOREACH member_name IN ARRAY ARRAY[
        'kdive-server-member',
        'kdive-worker-member',
        'kdive-reconciler-member',
        'kdive-witness-member'
    ] LOOP
        IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = member_name) THEN
            EXECUTE format(
                'CREATE ROLE %I LOGIN INHERIT NOSUPERUSER NOCREATEDB '
                'NOCREATEROLE NOREPLICATION NOBYPASSRLS',
                member_name
            );
        END IF;
        SELECT
            role.rolcanlogin
            AND role.rolinherit
            AND NOT role.rolsuper
            AND NOT role.rolcreatedb
            AND NOT role.rolcreaterole
            AND NOT role.rolreplication
            AND NOT role.rolbypassrls
        INTO attributes_match
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = member_name;
        IF NOT COALESCE(attributes_match, false) THEN
            RAISE EXCEPTION 'local runtime login % has incompatible attributes', member_name;
        END IF;
    END LOOP;
END
$bootstrap$;

ALTER ROLE "kdive-server-member"
    PASSWORD 'kdive-server-local'; -- pragma: allowlist secret
ALTER ROLE "kdive-worker-member"
    PASSWORD 'kdive-worker-local'; -- pragma: allowlist secret
ALTER ROLE "kdive-reconciler-member"
    PASSWORD 'kdive-reconciler-local'; -- pragma: allowlist secret
ALTER ROLE "kdive-witness-member"
    PASSWORD 'kdive-witness-local'; -- pragma: allowlist secret

WITH expected(member_name, capability_name) AS (
    VALUES
        ('kdive-server-member', 'kdive_server'),
        ('kdive-worker-member', 'kdive_worker'),
        ('kdive-reconciler-member', 'kdive_reconciler'),
        ('kdive-witness-member', 'kdive_lifecycle_witness')
)
SELECT format('REVOKE %I FROM %I', capability.rolname, member.rolname)
FROM pg_auth_members AS membership
JOIN pg_roles AS member ON member.oid = membership.member
JOIN pg_roles AS capability ON capability.oid = membership.roleid
LEFT JOIN expected
    ON expected.member_name = member.rolname
    AND expected.capability_name = capability.rolname
WHERE member.rolname IN (SELECT member_name FROM expected)
    AND capability.rolname IN (SELECT capability_name FROM expected)
    AND expected.member_name IS NULL
\gexec

WITH expected(member_name, capability_name) AS (
    VALUES
        ('kdive-server-member', 'kdive_server'),
        ('kdive-worker-member', 'kdive_worker'),
        ('kdive-reconciler-member', 'kdive_reconciler'),
        ('kdive-witness-member', 'kdive_lifecycle_witness')
)
SELECT format('GRANT %I TO %I', expected.capability_name, expected.member_name)
FROM expected
WHERE NOT EXISTS (
    SELECT 1
    FROM pg_auth_members AS membership
    JOIN pg_roles AS member ON member.oid = membership.member
    JOIN pg_roles AS capability ON capability.oid = membership.roleid
    WHERE member.rolname = expected.member_name
        AND capability.rolname = expected.capability_name
)
\gexec
-- The live-tier harness login (ADR-0769, #3196): development-only, least privilege. It reads every
-- public table (the protected-state snapshot and the carrier evidence reads) and makes only the
-- seeding writes. Grants converge on every run, so a table a later migration adds is covered.
DO $harness$
DECLARE
    harness_oid oid;
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'kdive-harness') THEN
        CREATE ROLE "kdive-harness" LOGIN NOINHERIT NOSUPERUSER NOCREATEDB
            NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
    SELECT role.oid INTO harness_oid
    FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = 'kdive-harness'
        AND role.rolcanlogin
        AND NOT role.rolsuper
        AND NOT role.rolcreatedb
        AND NOT role.rolcreaterole
        AND NOT role.rolreplication
        AND NOT role.rolbypassrls;
    IF harness_oid IS NULL THEN
        RAISE EXCEPTION 'local harness login kdive-harness has incompatible attributes'
            USING HINT = 'drop the role so the bootstrap re-creates it';
    END IF;
    IF NOT pg_has_role(current_user, harness_oid, 'MEMBER WITH ADMIN OPTION') THEN
        RAISE EXCEPTION 'local harness login kdive-harness is not administered by %', current_user
            USING HINT = 'drop the role so the bootstrap re-creates it as the migration owner';
    END IF;
    -- Member side only: the ADMIN grant its creator holds on it is the other direction.
    IF EXISTS (SELECT FROM pg_catalog.pg_auth_members WHERE member = harness_oid) THEN
        RAISE EXCEPTION 'local harness login kdive-harness must not be a member of any role'
            USING HINT = 'revoke its role memberships';
    END IF;
    IF EXISTS (SELECT FROM pg_catalog.pg_class WHERE relowner = harness_oid)
        OR EXISTS (SELECT FROM pg_catalog.pg_namespace WHERE nspowner = harness_oid) THEN
        RAISE EXCEPTION 'local harness login kdive-harness must own no relation or schema'
            USING HINT = 'reassign or drop what it owns';
    END IF;
END
$harness$;

ALTER ROLE "kdive-harness"
    PASSWORD 'kdive-harness-local'; -- pragma: allowlist secret

BEGIN;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM "kdive-harness";
GRANT USAGE ON SCHEMA public TO "kdive-harness";
GRANT SELECT ON ALL TABLES IN SCHEMA public TO "kdive-harness";
GRANT INSERT, UPDATE ON TABLE public.budgets, public.quotas, public.resources TO "kdive-harness";
GRANT INSERT ON TABLE public.allocations TO "kdive-harness";
COMMIT;
SQL
