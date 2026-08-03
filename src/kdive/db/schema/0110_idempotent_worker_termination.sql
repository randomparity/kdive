-- Exact worker-termination confirmation remains replayable after runtime API failure (#1803).

DROP FUNCTION public.terminate_worker_incarnation(text, text);

CREATE FUNCTION public.terminate_worker_incarnation(
    p_incarnation text,
    p_authority_kind text,
    p_authority_binding jsonb,
    p_outcome text
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_deadline timestamptz := clock_timestamp() + interval '5 seconds';
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_lifecycle_witness', 'member') THEN
        RAISE EXCEPTION 'lifecycle witness authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_incarnation IS NULL
       OR octet_length(p_incarnation) NOT BETWEEN 1 AND 512
       OR p_authority_kind NOT IN ('local', 'docker', 'kubernetes')
       OR p_authority_binding IS NULL
       OR octet_length(p_authority_binding::text) NOT BETWEEN 2 AND 2048
       OR p_outcome NOT IN ('succeeded', 'failed', 'killed') THEN
        RAISE EXCEPTION 'worker termination facts are invalid' USING ERRCODE = '22023';
    END IF;
    PERFORM set_config('lock_timeout', '5s', true);
    PERFORM set_config('statement_timeout', '5s', true);
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:worker-incarnation:' || p_incarnation, 1803)
    );
    IF clock_timestamp() >= v_deadline THEN
        RAISE EXCEPTION 'worker termination operation timed out' USING ERRCODE = '57014';
    END IF;
    UPDATE public.worker_incarnations
    SET state = 'terminated',
        terminated_at = clock_timestamp(),
        outcome = p_outcome,
        credential_envelope = NULL
    WHERE incarnation = p_incarnation
      AND authority_kind = p_authority_kind
      AND authority_binding = p_authority_binding
      AND state = 'active';
    IF FOUND THEN
        RETURN true;
    END IF;
    RETURN EXISTS (
        SELECT 1
        FROM public.worker_incarnations
        WHERE incarnation = p_incarnation
          AND authority_kind = p_authority_kind
          AND authority_binding = p_authority_binding
          AND state = 'terminated'
          AND outcome = p_outcome
    );
END
$$;

REVOKE ALL ON FUNCTION public.terminate_worker_incarnation(text, text, jsonb, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.terminate_worker_incarnation(text, text, jsonb, text)
    FROM kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness;
GRANT EXECUTE ON FUNCTION public.terminate_worker_incarnation(text, text, jsonb, text)
    TO kdive_lifecycle_witness;
