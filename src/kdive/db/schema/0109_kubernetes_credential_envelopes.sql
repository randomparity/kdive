-- Durable Kubernetes init delivery envelopes (ADR-0533, #1803).
ALTER TABLE public.worker_incarnations
    ADD COLUMN credential_envelope bytea,
    ADD COLUMN credential_acknowledged_at timestamptz,
    ADD CONSTRAINT worker_incarnations_envelope_bounded
        CHECK (credential_envelope IS NULL OR octet_length(credential_envelope) BETWEEN 1 AND 4096),
    ADD CONSTRAINT worker_incarnations_envelope_lifecycle
        CHECK (
            credential_envelope IS NULL
            OR (state = 'active' AND credential_acknowledged_at IS NULL)
        ),
    ADD CONSTRAINT worker_incarnations_acknowledgment_clears_envelope
        CHECK (credential_acknowledged_at IS NULL OR credential_envelope IS NULL);

CREATE FUNCTION register_kubernetes_worker_incarnation(
    p_incarnation text,
    p_authority_binding jsonb,
    p_credential_hash bytea,
    p_credential_envelope bytea,
    p_fence_protocol integer
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
       OR p_authority_binding IS NULL
       OR jsonb_typeof(p_authority_binding) <> 'object'
       OR octet_length(p_authority_binding::text) > 4096
       OR jsonb_typeof(p_authority_binding -> 'namespace') IS DISTINCT FROM 'string'
       OR jsonb_typeof(p_authority_binding -> 'name') IS DISTINCT FROM 'string'
       OR jsonb_typeof(p_authority_binding -> 'uid') IS DISTINCT FROM 'string'
       OR octet_length(p_authority_binding ->> 'namespace') NOT BETWEEN 1 AND 253
       OR octet_length(p_authority_binding ->> 'name') NOT BETWEEN 1 AND 253
       OR octet_length(p_authority_binding ->> 'uid') NOT BETWEEN 1 AND 253
       OR p_incarnation <> (
           'kubernetes:' || (p_authority_binding ->> 'namespace') || ':'
           || (p_authority_binding ->> 'name') || ':' || (p_authority_binding ->> 'uid')
       )
       OR p_credential_hash IS NULL
       OR octet_length(p_credential_hash) <> 32
       OR p_credential_envelope IS NULL
       OR octet_length(p_credential_envelope) NOT BETWEEN 1 AND 4096
       OR p_fence_protocol IS NULL
       OR p_fence_protocol <= 0 THEN
        RAISE EXCEPTION 'Kubernetes worker incarnation facts are invalid' USING ERRCODE = '22023';
    END IF;
    PERFORM set_config('lock_timeout', '5s', true);
    PERFORM set_config('statement_timeout', '5s', true);
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:worker-incarnation:' || p_incarnation, 1803)
    );
    IF clock_timestamp() >= v_deadline THEN
        RAISE EXCEPTION 'Kubernetes credential operation timed out' USING ERRCODE = '57014';
    END IF;
    INSERT INTO public.worker_incarnations (
        incarnation, authority_kind, authority_binding, credential_hash, credential_envelope,
        fence_protocol
    ) VALUES (
        p_incarnation, 'kubernetes', p_authority_binding, p_credential_hash, p_credential_envelope,
        p_fence_protocol
    ) ON CONFLICT (incarnation) DO NOTHING;
    IF FOUND THEN
        RETURN true;
    END IF;
    RETURN EXISTS (
        SELECT 1 FROM public.worker_incarnations
        WHERE incarnation = p_incarnation
          AND authority_kind = 'kubernetes'
          AND authority_binding = p_authority_binding
          AND fence_protocol = p_fence_protocol
          AND state = 'active'
        FOR UPDATE
    );
END
$$;

CREATE FUNCTION read_kubernetes_credential_envelope(p_incarnation text, p_authority_binding jsonb)
RETURNS bytea
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_deadline timestamptz := clock_timestamp() + interval '5 seconds';
    v_envelope bytea;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_lifecycle_witness', 'member') THEN
        RAISE EXCEPTION 'lifecycle witness authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_incarnation IS NULL OR p_authority_binding IS NULL THEN
        RAISE EXCEPTION 'Kubernetes credential lookup facts are invalid' USING ERRCODE = '22023';
    END IF;
    PERFORM set_config('lock_timeout', '5s', true);
    PERFORM set_config('statement_timeout', '5s', true);
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:worker-incarnation:' || p_incarnation, 1803)
    );
    IF clock_timestamp() >= v_deadline THEN
        RAISE EXCEPTION 'Kubernetes credential operation timed out' USING ERRCODE = '57014';
    END IF;
    SELECT credential_envelope INTO v_envelope
    FROM public.worker_incarnations
    WHERE incarnation = p_incarnation
      AND authority_kind = 'kubernetes'
      AND authority_binding = p_authority_binding
      AND state = 'active'
      AND credential_acknowledged_at IS NULL
    FOR UPDATE;
    RETURN v_envelope;
END
$$;

CREATE FUNCTION acknowledge_kubernetes_credential_envelope(
    p_incarnation text,
    p_authority_binding jsonb
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
    IF p_incarnation IS NULL OR p_authority_binding IS NULL THEN
        RAISE EXCEPTION 'Kubernetes credential acknowledgment facts are invalid' USING ERRCODE = '22023';
    END IF;
    PERFORM set_config('lock_timeout', '5s', true);
    PERFORM set_config('statement_timeout', '5s', true);
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:worker-incarnation:' || p_incarnation, 1803)
    );
    IF clock_timestamp() >= v_deadline THEN
        RAISE EXCEPTION 'Kubernetes credential operation timed out' USING ERRCODE = '57014';
    END IF;
    UPDATE public.worker_incarnations
    SET credential_envelope = NULL, credential_acknowledged_at = clock_timestamp()
    WHERE incarnation = p_incarnation
      AND authority_kind = 'kubernetes'
      AND authority_binding = p_authority_binding
      AND state = 'active'
      AND credential_envelope IS NOT NULL;
    IF FOUND THEN
        RETURN true;
    END IF;
    RETURN EXISTS (
        SELECT 1 FROM public.worker_incarnations
        WHERE incarnation = p_incarnation
          AND authority_kind = 'kubernetes'
          AND authority_binding = p_authority_binding
          AND state = 'active'
          AND credential_acknowledged_at IS NOT NULL
    );
END
$$;

CREATE OR REPLACE FUNCTION terminate_worker_incarnation(p_incarnation text, p_outcome text)
RETURNS boolean
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
       OR p_outcome NOT IN ('succeeded', 'failed', 'killed') THEN
        RAISE EXCEPTION 'worker termination facts are invalid' USING ERRCODE = '22023';
    END IF;
    PERFORM set_config('lock_timeout', '5s', true);
    PERFORM set_config('statement_timeout', '5s', true);
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:worker-incarnation:' || p_incarnation, 1803)
    );
    IF clock_timestamp() >= v_deadline THEN
        RAISE EXCEPTION 'Kubernetes credential operation timed out' USING ERRCODE = '57014';
    END IF;
    UPDATE public.worker_incarnations
    SET state = 'terminated',
        terminated_at = clock_timestamp(),
        outcome = p_outcome,
        credential_envelope = NULL
    WHERE incarnation = p_incarnation AND state = 'active';
    RETURN FOUND;
END
$$;

REVOKE ALL ON FUNCTION register_kubernetes_worker_incarnation(text, jsonb, bytea, bytea, integer)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION register_kubernetes_worker_incarnation(text, jsonb, bytea, bytea, integer)
    FROM kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness;
REVOKE ALL ON FUNCTION read_kubernetes_credential_envelope(text, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION read_kubernetes_credential_envelope(text, jsonb)
    FROM kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness;
REVOKE ALL ON FUNCTION acknowledge_kubernetes_credential_envelope(text, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION acknowledge_kubernetes_credential_envelope(text, jsonb)
    FROM kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness;
REVOKE ALL ON FUNCTION terminate_worker_incarnation(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION terminate_worker_incarnation(text, text)
    FROM kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness;
GRANT EXECUTE ON FUNCTION register_kubernetes_worker_incarnation(text, jsonb, bytea, bytea, integer)
    TO kdive_lifecycle_witness;
GRANT EXECUTE ON FUNCTION read_kubernetes_credential_envelope(text, jsonb)
    TO kdive_lifecycle_witness;
GRANT EXECUTE ON FUNCTION acknowledge_kubernetes_credential_envelope(text, jsonb)
    TO kdive_lifecycle_witness;
GRANT EXECUTE ON FUNCTION terminate_worker_incarnation(text, text) TO kdive_lifecycle_witness;
