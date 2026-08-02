-- Bounded role-gated authority transitions (ADR-0533, #1803).
-- Every incarnation transition takes the same transaction-scoped advisory lock. Hash collisions
-- only over-serialize unrelated incarnations; they cannot weaken the fence.

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
    IF p_incarnation IS NULL
       OR octet_length(p_incarnation) NOT BETWEEN 1 AND 512
       OR p_authority_kind NOT IN ('local', 'docker', 'kubernetes')
       OR p_authority_binding IS NULL
       OR jsonb_typeof(p_authority_binding) <> 'object'
       OR octet_length(p_authority_binding::text) > 4096
       OR p_credential_hash IS NULL
       OR octet_length(p_credential_hash) <> 32
       OR p_fence_protocol IS NULL
       OR p_fence_protocol <= 0 THEN
        RAISE EXCEPTION 'worker incarnation facts are invalid' USING ERRCODE = '22023';
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:worker-incarnation:' || p_incarnation, 1803)
    );
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
        FOR UPDATE
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
DECLARE
    v_incarnation text;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_credential_hash IS NULL OR octet_length(p_credential_hash) <> 32 THEN
        RAISE EXCEPTION 'worker credential hash is invalid' USING ERRCODE = '22023';
    END IF;
    SELECT w.incarnation INTO v_incarnation
    FROM public.worker_incarnations AS w
    WHERE w.credential_hash = p_credential_hash;
    IF v_incarnation IS NULL THEN
        RETURN;
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:worker-incarnation:' || v_incarnation, 1803)
    );
    RETURN QUERY
    SELECT w.incarnation, w.authority_kind, w.authority_binding, w.fence_protocol
    FROM public.worker_incarnations AS w
    WHERE w.incarnation = v_incarnation
      AND w.credential_hash = p_credential_hash
      AND w.state = 'active'
    FOR UPDATE;
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
    IF p_incarnation IS NULL
       OR octet_length(p_incarnation) NOT BETWEEN 1 AND 512
       OR p_outcome NOT IN ('succeeded', 'failed', 'killed') THEN
        RAISE EXCEPTION 'worker termination facts are invalid' USING ERRCODE = '22023';
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:worker-incarnation:' || p_incarnation, 1803)
    );
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
    p_credential_hash bytea
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_holder text;
    v_claim_attempt integer;
    v_claim_lease timestamptz;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_use_id IS NULL
       OR p_investigation_id IS NULL
       OR p_generation IS NULL
       OR p_job_id IS NULL
       OR p_attempt IS NULL
       OR p_attempt <= 0
       OR p_credential_hash IS NULL
       OR octet_length(p_credential_hash) <> 32 THEN
        RAISE EXCEPTION 'build-use facts are invalid' USING ERRCODE = '22023';
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
    SELECT j.attempt, j.lease_expires_at INTO v_claim_attempt, v_claim_lease
    FROM public.jobs AS j
    WHERE j.id = p_job_id
      AND j.state = 'running'
      AND j.worker_id = v_holder
      AND j.attempt = p_attempt
      AND j.lease_expires_at IS NOT NULL
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    INSERT INTO public.investigation_build_uses (
        use_id, investigation_id, generation, job_id, attempt, holder_worker_id, lease_expires_at
    ) VALUES (
        p_use_id,
        p_investigation_id,
        p_generation,
        p_job_id,
        v_claim_attempt,
        v_holder,
        v_claim_lease
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
      AND j.state = 'running'
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

CREATE FUNCTION recover_investigation_build_use(
    p_use_id uuid,
    p_project text,
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
    v_holder text;
    v_project text;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_reconciler', 'member') THEN
        RAISE EXCEPTION 'reconciler authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_use_id IS NULL
       OR p_project IS NULL
       OR length(btrim(p_project)) = 0
       OR p_recovered_by IS NULL
       OR octet_length(p_recovered_by) NOT BETWEEN 1 AND 255
       OR length(btrim(p_recovered_by)) = 0
       OR p_reason IS NULL
       OR octet_length(p_reason) NOT BETWEEN 1 AND 512
       OR length(btrim(p_reason)) = 0 THEN
        RAISE EXCEPTION 'recovery facts are invalid' USING ERRCODE = '22023';
    END IF;
    SELECT u.holder_worker_id INTO v_holder
    FROM public.investigation_build_uses AS u
    JOIN public.investigation_builds AS b
      ON b.investigation_id = u.investigation_id AND b.generation = u.generation
    JOIN public.investigations AS i ON i.id = b.investigation_id
    WHERE u.use_id = p_use_id AND i.project = p_project;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:worker-incarnation:' || v_holder, 1803)
    );
    SELECT u.* INTO v_use
    FROM public.investigation_build_uses AS u
    JOIN public.investigation_builds AS b
      ON b.investigation_id = u.investigation_id AND b.generation = u.generation
    JOIN public.investigations AS i ON i.id = b.investigation_id
    WHERE u.use_id = p_use_id
      AND u.holder_worker_id = v_holder
      AND i.project = p_project
    FOR UPDATE OF u, b, i;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    v_project := p_project;
    SELECT w.* INTO v_incarnation
    FROM public.worker_incarnations AS w
    WHERE w.incarnation = v_use.holder_worker_id AND w.state = 'terminated'
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    INSERT INTO public.investigation_build_use_recoveries (
        use_id, project, investigation_id, generation, job_id, attempt, holder_worker_id,
        recovered_by, evidence, reason, authority_kind, authority_binding,
        termination_outcome, terminated_at
    ) VALUES (
        v_use.use_id,
        v_project,
        v_use.investigation_id,
        v_use.generation,
        v_use.job_id,
        v_use.attempt,
        v_use.holder_worker_id,
        p_recovered_by,
        v_incarnation.authority_kind || ': durable exact-incarnation termination',
        p_reason,
        v_incarnation.authority_kind,
        v_incarnation.authority_binding,
        v_incarnation.outcome,
        v_incarnation.terminated_at
    );
    DELETE FROM public.investigation_build_uses
    WHERE use_id = v_use.use_id
      AND investigation_id = v_use.investigation_id
      AND generation = v_use.generation
      AND job_id = v_use.job_id
      AND attempt = v_use.attempt
      AND holder_worker_id = v_use.holder_worker_id;
    RETURN FOUND;
END
$$;

REVOKE ALL ON FUNCTION register_worker_incarnation(text, text, jsonb, bytea, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION authenticate_worker_incarnation(bytea) FROM PUBLIC;
REVOKE ALL ON FUNCTION terminate_worker_incarnation(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION acquire_investigation_build_use(uuid, uuid, uuid, uuid, integer, bytea) FROM PUBLIC;
REVOKE ALL ON FUNCTION release_investigation_build_use(uuid, bytea) FROM PUBLIC;
REVOKE ALL ON FUNCTION recover_investigation_build_use(uuid, text, text, text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION register_worker_incarnation(text, text, jsonb, bytea, integer)
    TO kdive_lifecycle_witness;
GRANT EXECUTE ON FUNCTION terminate_worker_incarnation(text, text) TO kdive_lifecycle_witness;
GRANT EXECUTE ON FUNCTION authenticate_worker_incarnation(bytea) TO kdive_worker;
GRANT EXECUTE ON FUNCTION acquire_investigation_build_use(uuid, uuid, uuid, uuid, integer, bytea)
    TO kdive_worker;
GRANT EXECUTE ON FUNCTION release_investigation_build_use(uuid, bytea) TO kdive_worker;
GRANT EXECUTE ON FUNCTION recover_investigation_build_use(uuid, text, text, text)
    TO kdive_reconciler;
