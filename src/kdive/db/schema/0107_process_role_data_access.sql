-- Ordinary runtime data access for separate process roles (ADR-0533, #1803).
--
-- The current server, worker, and reconciler share repository modules across the application
-- schema, so their minimum deployable boundary is CRUD on every ordinary table. The guarded
-- worker-incarnation and investigation-build-use evidence tables remain function-only, and the
-- migration ledger remains migration-owner-only. The lifecycle witness receives no ordinary
-- data access. This migration intentionally grants only relations that already exist: a future
-- migration that creates a table or sequence must declare its process-role access explicitly.
DO $$
DECLARE
    v_relation record;
BEGIN
    FOR v_relation IN
        SELECT namespace.nspname AS schema_name, relation.relname AS relation_name
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = 'public'
          AND relation.relkind IN ('r', 'p')
          AND relation.relname NOT IN (
              'schema_migrations',
              'worker_incarnations',
              'investigation_build_uses',
              'investigation_build_use_recoveries'
          )
    LOOP
        EXECUTE format(
            'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %I.%I '
            'TO kdive_server, kdive_worker, kdive_reconciler',
            v_relation.schema_name,
            v_relation.relation_name
        );
        EXECUTE format(
            'REVOKE ALL ON TABLE %I.%I FROM kdive_lifecycle_witness',
            v_relation.schema_name,
            v_relation.relation_name
        );
    END LOOP;

    FOR v_relation IN
        SELECT namespace.nspname AS schema_name, relation.relname AS relation_name
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = 'public'
          AND relation.relkind = 'S'
    LOOP
        EXECUTE format(
            'GRANT USAGE, SELECT, UPDATE ON SEQUENCE %I.%I '
            'TO kdive_server, kdive_worker, kdive_reconciler',
            v_relation.schema_name,
            v_relation.relation_name
        );
        EXECUTE format(
            'REVOKE ALL ON SEQUENCE %I.%I FROM kdive_lifecycle_witness',
            v_relation.schema_name,
            v_relation.relation_name
        );
    END LOOP;
END
$$;

REVOKE ALL ON TABLE public.schema_migrations FROM
    kdive_server,
    kdive_worker,
    kdive_reconciler,
    kdive_lifecycle_witness;
REVOKE ALL ON TABLE
    public.worker_incarnations,
    public.investigation_build_uses,
    public.investigation_build_use_recoveries
FROM kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness;
