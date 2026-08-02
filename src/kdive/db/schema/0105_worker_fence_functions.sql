-- Bounded role-gated authority transitions (ADR-0533, #1803).

CREATE FUNCTION register_worker_incarnation(
    p_incarnation text,
    p_authority_kind text,
    p_authority_binding jsonb,
    p_credential_hash bytea,
    p_fence_protocol integer
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_lifecycle_witness', 'member') THEN
        RAISE EXCEPTION 'lifecycle witness authority is required' USING ERRCODE = '42501';
    END IF;
    IF length(p_incarnation) NOT BETWEEN 1 AND 512
       OR p_authority_kind NOT IN ('local', 'docker', 'kubernetes')
       OR jsonb_typeof(p_authority_binding) <> 'object'
       OR octet_length(p_credential_hash) <> 32
       OR p_fence_protocol <= 0 THEN
        RAISE EXCEPTION 'worker incarnation facts are invalid' USING ERRCODE = '22023';
    END IF;
    INSERT INTO public.worker_incarnations (
        incarnation, authority_kind, authority_binding, credential_hash, fence_protocol
    ) VALUES (
        p_incarnation, p_authority_kind, p_authority_binding, p_credential_hash, p_fence_protocol
    ) ON CONFLICT (incarnation) DO NOTHING;
    IF NOT FOUND AND NOT EXISTS (
        SELECT 1 FROM public.worker_incarnations
        WHERE incarnation = p_incarnation
          AND authority_kind = p_authority_kind
          AND authority_binding = p_authority_binding
          AND credential_hash = p_credential_hash
          AND fence_protocol = p_fence_protocol
          AND state = 'active'
    ) THEN
        RAISE EXCEPTION 'worker incarnation conflicts with durable facts' USING ERRCODE = '23505';
    END IF;
END
$$;

CREATE FUNCTION authenticate_worker_incarnation(p_credential_hash bytea)
RETURNS TABLE (
    incarnation text,
    authority_kind text,
    authority_binding jsonb,
    fence_protocol integer
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    IF octet_length(p_credential_hash) <> 32 THEN
        RAISE EXCEPTION 'worker credential hash is invalid' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    SELECT w.incarnation, w.authority_kind, w.authority_binding, w.fence_protocol
    FROM public.worker_incarnations AS w
    WHERE w.credential_hash = p_credential_hash AND w.state = 'active';
END
$$;

CREATE FUNCTION terminate_worker_incarnation(p_incarnation text, p_outcome text)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_lifecycle_witness', 'member') THEN
        RAISE EXCEPTION 'lifecycle witness authority is required' USING ERRCODE = '42501';
    END IF;
    IF length(p_incarnation) NOT BETWEEN 1 AND 512
       OR p_outcome NOT IN ('succeeded', 'failed', 'killed') THEN
        RAISE EXCEPTION 'worker termination facts are invalid' USING ERRCODE = '22023';
    END IF;
    UPDATE public.worker_incarnations
    SET state = 'terminated', terminated_at = clock_timestamp(), outcome = p_outcome
    WHERE incarnation = p_incarnation AND state = 'active';
    RETURN FOUND;
END
$$;

CREATE FUNCTION acquire_investigation_build_use(
    p_use_id uuid,
    p_investigation_id uuid,
    p_generation uuid,
    p_job_id uuid,
    p_attempt integer,
    p_credential_hash bytea,
    p_lease_expires_at timestamptz
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_holder text;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_attempt <= 0 OR octet_length(p_credential_hash) <> 32 THEN
        RAISE EXCEPTION 'build-use facts are invalid' USING ERRCODE = '22023';
    END IF;
    SELECT w.incarnation INTO v_holder
    FROM public.worker_incarnations AS w
    WHERE w.credential_hash = p_credential_hash AND w.state = 'active';
    IF v_holder IS NULL THEN
        RETURN false;
    END IF;
    INSERT INTO public.investigation_build_uses (
        use_id, investigation_id, generation, job_id, attempt, holder_worker_id, lease_expires_at
    ) VALUES (
        p_use_id, p_investigation_id, p_generation, p_job_id, p_attempt, v_holder, p_lease_expires_at
    );
    RETURN true;
END
$$;

CREATE FUNCTION release_investigation_build_use(p_use_id uuid, p_credential_hash bytea)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    DELETE FROM public.investigation_build_uses AS u
    USING public.worker_incarnations AS w
    WHERE u.use_id = p_use_id
      AND w.credential_hash = p_credential_hash
      AND w.state = 'active'
      AND u.holder_worker_id = w.incarnation;
    RETURN FOUND;
END
$$;

CREATE FUNCTION recover_investigation_build_use(
    p_use_id uuid,
    p_recovered_by text,
    p_reason text
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_use public.investigation_build_uses%ROWTYPE;
    v_incarnation public.worker_incarnations%ROWTYPE;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_reconciler', 'member') THEN
        RAISE EXCEPTION 'reconciler authority is required' USING ERRCODE = '42501';
    END IF;
    IF length(p_recovered_by) NOT BETWEEN 1 AND 255
       OR length(p_reason) NOT BETWEEN 1 AND 512 THEN
        RAISE EXCEPTION 'recovery facts are invalid' USING ERRCODE = '22023';
    END IF;
    SELECT u.* INTO v_use FROM public.investigation_build_uses AS u WHERE u.use_id = p_use_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    SELECT w.* INTO v_incarnation
    FROM public.worker_incarnations AS w
    WHERE w.incarnation = v_use.holder_worker_id AND w.state = 'terminated'
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    INSERT INTO public.investigation_build_use_recoveries (
        use_id, investigation_id, generation, job_id, attempt, holder_worker_id,
        recovered_by, evidence, reason, authority_kind, authority_binding,
        termination_outcome, terminated_at
    ) VALUES (
        v_use.use_id, v_use.investigation_id, v_use.generation, v_use.job_id, v_use.attempt,
        v_use.holder_worker_id, p_recovered_by,
        v_incarnation.authority_kind || ': durable exact-incarnation termination', p_reason,
        v_incarnation.authority_kind, v_incarnation.authority_binding,
        v_incarnation.outcome, v_incarnation.terminated_at
    );
    DELETE FROM public.investigation_build_uses WHERE use_id = p_use_id;
    RETURN true;
END
$$;

REVOKE ALL ON FUNCTION register_worker_incarnation(text, text, jsonb, bytea, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION authenticate_worker_incarnation(bytea) FROM PUBLIC;
REVOKE ALL ON FUNCTION terminate_worker_incarnation(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION acquire_investigation_build_use(uuid, uuid, uuid, uuid, integer, bytea, timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION release_investigation_build_use(uuid, bytea) FROM PUBLIC;
REVOKE ALL ON FUNCTION recover_investigation_build_use(uuid, text, text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION register_worker_incarnation(text, text, jsonb, bytea, integer) TO kdive_lifecycle_witness;
GRANT EXECUTE ON FUNCTION terminate_worker_incarnation(text, text) TO kdive_lifecycle_witness;
GRANT EXECUTE ON FUNCTION authenticate_worker_incarnation(bytea) TO kdive_worker;
GRANT EXECUTE ON FUNCTION acquire_investigation_build_use(uuid, uuid, uuid, uuid, integer, bytea, timestamptz) TO kdive_worker;
GRANT EXECUTE ON FUNCTION release_investigation_build_use(uuid, bytea) TO kdive_worker;
GRANT EXECUTE ON FUNCTION recover_investigation_build_use(uuid, text, text) TO kdive_reconciler;
