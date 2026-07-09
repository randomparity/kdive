-- 0062_drop_server_build_tables.sql — retire the server-build lane's orphaned tables.
-- Forward-only (ADR-0015). The server kernel-build execution, build-host fleet, build-config
-- catalog, and inventory build-host machinery have all been removed from the code, so these
-- tables have no remaining reader or writer. Drop dependents BEFORE build_hosts because their
-- FKs REFERENCE build_hosts(id) (build_host_leases ON DELETE RESTRICT, buildhost_agent_probe_guests
-- ON DELETE CASCADE). IF EXISTS keeps a partially-applied schema idempotent.
DROP TABLE IF EXISTS build_config_catalog;
DROP TABLE IF EXISTS buildhost_agent_probe_guests;
DROP TABLE IF EXISTS build_host_leases;
DROP TABLE IF EXISTS build_hosts;
-- The JobKind enum values BUILD, BUILD_INSTALL_BOOT are intentionally left in place: Postgres
-- cannot drop a value from an existing enum without recreating the type. They stay inert — no code
-- path enqueues them after the server-build lane removal.
