-- Idempotent owner bootstrap/convergence for the local Compose reference only (ADR-0533).
SELECT 'CREATE ROLE "kdive-migration"'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'kdive-migration')
\gexec
ALTER ROLE "kdive-migration" LOGIN NOINHERIT NOSUPERUSER NOCREATEDB CREATEROLE
    NOREPLICATION NOBYPASSRLS PASSWORD 'kdive-migration-local'; -- pragma: allowlist secret
ALTER DATABASE kdive OWNER TO "kdive-migration";
ALTER SCHEMA public OWNER TO "kdive-migration";
SELECT format(
    'ALTER %s %I.%I OWNER TO "kdive-migration"',
    CASE c.relkind
        WHEN 'S' THEN 'SEQUENCE'
        WHEN 'f' THEN 'FOREIGN TABLE'
        WHEN 'm' THEN 'MATERIALIZED VIEW'
        WHEN 'v' THEN 'VIEW'
        ELSE 'TABLE'
    END,
    n.nspname,
    c.relname
)
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relkind IN ('S', 'f', 'm', 'p', 'r', 'v')
  AND pg_get_userbyid(c.relowner) = 'kdive'
ORDER BY c.relname
\gexec
SELECT format(
    'ALTER %s %I.%I(%s) OWNER TO "kdive-migration"',
    CASE p.prokind WHEN 'a' THEN 'AGGREGATE' WHEN 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END,
    n.nspname,
    p.proname,
    pg_get_function_identity_arguments(p.oid)
)
FROM pg_proc AS p
JOIN pg_namespace AS n ON n.oid = p.pronamespace
WHERE n.nspname = 'public'
  AND pg_get_userbyid(p.proowner) = 'kdive'
ORDER BY p.proname, pg_get_function_identity_arguments(p.oid)
\gexec
SELECT format('GRANT %I TO "kdive-migration" WITH ADMIN OPTION', role.rolname)
FROM pg_roles AS role
WHERE role.rolname IN (
    'kdive_server',
    'kdive_worker',
    'kdive_reconciler',
    'kdive_lifecycle_witness'
)
ORDER BY role.rolname
\gexec
