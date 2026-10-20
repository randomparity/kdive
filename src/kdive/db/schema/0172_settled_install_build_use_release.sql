-- Cooperative owner release after install provider settlement (ADR-0784, #3241).
CREATE OR REPLACE FUNCTION release_investigation_build_use(p_use_id uuid, p_credential_hash bytea)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_holder text;
    v_job_id uuid;
    v_attempt integer;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_use_id IS NULL
       OR p_credential_hash IS NULL
       OR octet_length(p_credential_hash) <> 32 THEN
        RAISE EXCEPTION 'build-use release facts are invalid' USING ERRCODE = '22023';
    END IF;
    SELECT w.incarnation INTO v_holder
    FROM public.worker_incarnations AS w
    WHERE w.credential_hash = p_credential_hash;
    IF v_holder IS NULL THEN
        RETURN false;
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:worker-incarnation:' || v_holder, 1803)
    );
    PERFORM 1 FROM public.worker_incarnations AS w
    WHERE w.incarnation = v_holder
      AND w.credential_hash = p_credential_hash
      AND w.state = 'active'
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    SELECT u.job_id, u.attempt INTO v_job_id, v_attempt
    FROM public.investigation_build_uses AS u
    WHERE u.use_id = p_use_id AND u.holder_worker_id = v_holder
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    PERFORM 1 FROM public.jobs AS j
    WHERE j.id = v_job_id
      AND j.state IN ('running', 'canceled')
      AND j.worker_id = v_holder
      AND j.attempt = v_attempt
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    DELETE FROM public.investigation_build_uses
    WHERE use_id = p_use_id
      AND job_id = v_job_id
      AND attempt = v_attempt
      AND holder_worker_id = v_holder;
    RETURN FOUND;
END
$$;
