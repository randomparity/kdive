-- Clean-database bootstrap for the local Compose reference only (ADR-0533).
CREATE ROLE "kdive-migration" LOGIN NOINHERIT NOSUPERUSER NOCREATEDB CREATEROLE
    NOREPLICATION NOBYPASSRLS PASSWORD 'kdive-migration-local'; -- pragma: allowlist secret
ALTER DATABASE kdive OWNER TO "kdive-migration";
ALTER SCHEMA public OWNER TO "kdive-migration";
