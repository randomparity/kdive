-- 0152_worker_system_mutation_discharge.sql — worker/reconciler bulk terminal-escape discharge
--
-- ADR-0629. `remote_module_attempt_obligations` grants kdive_worker and kdive_reconciler
-- SELECT only (0126:230-232), but both roles reach the bulk discharge on the System teardown
-- reclaim path, so the direct UPDATE failed with 42501 (#2302). 0132 and 0147 composed this
-- same write into a definer function they already had; that path has none, so it gets one.

CREATE FUNCTION public.discharge_system_mutation_obligations(p_system_id uuid)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_discharged integer;
BEGIN
    -- The EXECUTE grant below already restricts the callers. This gate is the second layer,
    -- so a later grant widened by accident does not by itself widen who may write.
    IF NOT (pg_catalog.pg_has_role(session_user, 'kdive_worker', 'member')
            OR pg_catalog.pg_has_role(session_user, 'kdive_reconciler', 'member')) THEN
        RAISE EXCEPTION 'worker or reconciler authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_system_id IS NULL THEN
        RAISE EXCEPTION 'system id is required' USING ERRCODE = '22023';
    END IF;

    -- The caller holds the System advisory lock for the surrounding transaction; the lock
    -- helper's key space is not reachable from SQL, so it is deliberately not retaken here.
    UPDATE public.remote_module_attempt_obligations
    SET mutation_discharged_at = pg_catalog.now(),
        mutation_discharge_reason = 'terminal_escape'
    WHERE system_id = p_system_id AND mutation_discharged_at IS NULL;
    GET DIAGNOSTICS v_discharged = ROW_COUNT;
    RETURN v_discharged;
END
$$;

REVOKE ALL ON FUNCTION public.discharge_system_mutation_obligations(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.discharge_system_mutation_obligations(uuid)
    TO kdive_worker, kdive_reconciler;
