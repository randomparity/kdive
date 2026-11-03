-- 0183_authority_first_activation_proof.sql — #3423, ADR-0754 (2026-10-30).
-- Forward-only (ADR-0015). Allocation release after a torn-down authority System is admitted on
-- its recorded first activation, not on any activation row: an unclaimed row left by a claim
-- blocked by a requested teardown is not a first activation.

CREATE FUNCTION public.authority_system_torn_down_after_activation(p_system_id uuid)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' STABLE AS $$
BEGIN
    IF NOT (pg_has_role(session_user, 'kdive_server', 'member')
            OR pg_has_role(session_user, 'kdive_reconciler', 'member')) THEN
        RAISE EXCEPTION 'control authority is required' USING ERRCODE = '42501';
    END IF;
    RETURN EXISTS (
        SELECT 1 FROM public.authority_system_ownership
        WHERE system_id = p_system_id
          AND state = 'torn-down' AND first_activation_id IS NOT NULL
    );
END
$$;

REVOKE ALL ON FUNCTION public.authority_system_torn_down_after_activation(uuid)
FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
     kdive_provider_authority;
GRANT EXECUTE ON FUNCTION public.authority_system_torn_down_after_activation(uuid)
TO kdive_server, kdive_reconciler;
