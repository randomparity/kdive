-- 0134_remote_module_worker_evidence.sql — narrow worker-owned terminal/reap writes

CREATE FUNCTION public.commit_worker_remote_module_evidence(
    p_job_id uuid,
    p_credential_hash bytea,
    p_job_attempt integer,
    p_system_id uuid,
    p_run_id uuid,
    p_operation_nonce text,
    p_action text,
    p_evidence jsonb DEFAULT NULL
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_incarnation text;
    v_payload jsonb;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_job_id IS NULL OR p_credential_hash IS NULL
       OR octet_length(p_credential_hash) <> 32 OR p_job_attempt IS NULL OR p_job_attempt < 1
       OR p_system_id IS NULL OR p_run_id IS NULL OR p_operation_nonce IS NULL
       OR p_operation_nonce !~ '^[0-9a-f]{32}$'
       OR p_action IS NULL OR p_action NOT IN ('record-terminal', 'discharge-reap') THEN
        RAISE EXCEPTION 'remote module worker evidence facts are invalid'
            USING ERRCODE = '22023';
    END IF;

    SELECT worker.incarnation INTO v_incarnation
    FROM public.worker_incarnations AS worker
    WHERE worker.credential_hash = p_credential_hash;
    IF v_incarnation IS NULL THEN
        RETURN false;
    END IF;
    PERFORM pg_advisory_xact_lock(
        pg_catalog.hashtextextended('kdive:system:' || p_system_id::text, 2125)
    );
    PERFORM pg_advisory_xact_lock(
        pg_catalog.hashtextextended('kdive:worker-incarnation:' || v_incarnation, 1803)
    );
    PERFORM 1 FROM public.worker_incarnations AS worker
    WHERE worker.incarnation = v_incarnation
      AND worker.credential_hash = p_credential_hash
      AND worker.state = 'active'
      AND worker.fence_protocol = 4
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;

    SELECT job.payload INTO v_payload
    FROM public.jobs AS job
    WHERE job.id = p_job_id
      AND job.worker_id = v_incarnation
      AND job.attempt = p_job_attempt
      AND job.state = 'running'
      AND job.kind IN ('boot', 'teardown')
      AND job.lease_expires_at IS NOT NULL
      AND job.lease_expires_at > pg_catalog.clock_timestamp()
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    IF v_payload -> 'remote_module_attempt_v1' IS DISTINCT FROM
       pg_catalog.jsonb_build_object(
           'schema', 'module-attempt-preparation-request-v1',
           'module_attempt_obligation', pg_catalog.jsonb_build_object(
               'schema', 'module-attempt-obligation-receipt-v1',
               'system_id', p_system_id::text,
               'run_id', p_run_id::text,
               'operation_nonce', p_operation_nonce
           )
       ) THEN
        RETURN false;
    END IF;

    PERFORM 1 FROM public.jobs AS job
    WHERE job.id = p_job_id
      AND job.worker_id = v_incarnation
      AND job.attempt = p_job_attempt
      AND job.state = 'running'
      AND job.lease_expires_at > pg_catalog.clock_timestamp();
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    PERFORM 1 FROM public.remote_module_attempt_obligations AS obligation
    WHERE obligation.system_id = p_system_id
      AND obligation.run_id = p_run_id
      AND obligation.operation_nonce = p_operation_nonce
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;

    IF p_action = 'record-terminal' THEN
        IF p_evidence IS NULL OR pg_catalog.jsonb_typeof(p_evidence) <> 'object' THEN
            RAISE EXCEPTION 'remote module terminal evidence is invalid'
                USING ERRCODE = '22023';
        END IF;
        UPDATE public.remote_module_attempt_obligations
        SET terminal_operation = p_evidence -> 'terminal_operation',
            terminal_operation_identity = p_evidence ->> 'terminal_operation_identity',
            terminal_result = p_evidence -> 'terminal_result',
            terminal_result_identity = p_evidence ->> 'terminal_result_identity',
            baseline_operation_identity = p_evidence ->> 'baseline_operation_identity',
            baseline_result_identity = p_evidence ->> 'baseline_result_identity',
            installed_entry_count = (p_evidence ->> 'installed_entry_count')::integer,
            installed_content_bytes = (p_evidence ->> 'installed_content_bytes')::bigint,
            recovery_reference = p_evidence -> 'recovery_reference',
            reap_opened_at = COALESCE(reap_opened_at, pg_catalog.now())
        WHERE system_id = p_system_id AND run_id = p_run_id
          AND operation_nonce = p_operation_nonce
          AND mutation_discharged_at IS NULL;
    ELSE
        IF p_evidence IS NOT NULL THEN
            RAISE EXCEPTION 'reap discharge does not accept evidence' USING ERRCODE = '22023';
        END IF;
        UPDATE public.remote_module_attempt_obligations
        SET reap_discharged_at = COALESCE(reap_discharged_at, pg_catalog.now())
        WHERE system_id = p_system_id AND run_id = p_run_id
          AND operation_nonce = p_operation_nonce
          AND reap_opened_at IS NOT NULL AND terminal_operation IS NOT NULL;
    END IF;
    RETURN FOUND;
END
$$;

REVOKE ALL ON FUNCTION public.commit_worker_remote_module_evidence(
    uuid, bytea, integer, uuid, uuid, text, text, jsonb
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.commit_worker_remote_module_evidence(
    uuid, bytea, integer, uuid, uuid, text, text, jsonb
) TO kdive_worker;
