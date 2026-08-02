-- Runtime roles are separate non-login capabilities (ADR-0533, #1803).
-- A cluster-global name collision is safe only when it is already the exact capability shape.
DO $$
DECLARE
    v_role text;
    v_attributes_match boolean;
BEGIN
    FOREACH v_role IN ARRAY ARRAY[
        'kdive_server',
        'kdive_worker',
        'kdive_reconciler',
        'kdive_lifecycle_witness'
    ] LOOP
        SELECT
            NOT r.rolcanlogin
            AND NOT r.rolinherit
            AND NOT r.rolsuper
            AND NOT r.rolcreaterole
            AND NOT r.rolcreatedb
            AND NOT r.rolreplication
            AND NOT r.rolbypassrls
            AND NOT EXISTS (
                SELECT 1 FROM pg_catalog.pg_auth_members AS membership
                WHERE membership.member = r.oid
            )
        INTO v_attributes_match
        FROM pg_catalog.pg_roles AS r
        WHERE r.rolname = v_role;

        IF NOT FOUND THEN
            BEGIN
                EXECUTE format(
                    'CREATE ROLE %I NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB '
                    'NOCREATEROLE NOREPLICATION NOBYPASSRLS',
                    v_role
                );
            EXCEPTION
                WHEN unique_violation OR duplicate_object THEN
                    -- Another database can migrate against the same cluster concurrently.
                    NULL;
            END;

            SELECT
                NOT r.rolcanlogin
                AND NOT r.rolinherit
                AND NOT r.rolsuper
                AND NOT r.rolcreaterole
                AND NOT r.rolcreatedb
                AND NOT r.rolreplication
                AND NOT r.rolbypassrls
                AND NOT EXISTS (
                    SELECT 1 FROM pg_catalog.pg_auth_members AS membership
                    WHERE membership.member = r.oid
                )
            INTO v_attributes_match
            FROM pg_catalog.pg_roles AS r
            WHERE r.rolname = v_role;
        END IF;

        IF NOT FOUND OR NOT COALESCE(v_attributes_match, false) THEN
            RAISE EXCEPTION 'runtime role % has incompatible attributes or memberships', v_role;
        END IF;
        -- Establish a database dependency before validating the next cluster-global role. This
        -- closes the post-validation window in which another database could drop the role.
        EXECUTE format('GRANT USAGE ON SCHEMA public TO %I', v_role);
    END LOOP;
END
$$;

REVOKE ALL ON TABLE public.worker_incarnations FROM PUBLIC;
REVOKE ALL ON TABLE public.investigation_build_uses FROM PUBLIC;
REVOKE ALL ON TABLE public.investigation_build_use_recoveries FROM PUBLIC;
