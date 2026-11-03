-- 0187_remote_module_attempt_adopted_preparation.sql — a retry generation that adopted an earlier
-- generation's committed preparation may open its remote module attempt (ADR-0585 amendment,
-- #3370).
--
-- 0146 admitted an attempt only when the CURRENT generation had its own materialize/result_committed
-- audit row. A retry generation adopts the earlier generation's preparation and commits no such row,
-- so the gate returned NULL for every generation above 1 and the retried PREPARE ended superseded.
-- This replaces the function with the same signature, grants and fences. The committed row may now
-- belong to any generation up to the current one, provided it names the same system, activation, run,
-- plan identity, provider kind and authority instance as the current authority. The activation row
-- must still be preparing with its materialization recorded. A generation with no committed or
-- adopted preparation still finds no row and is refused.

CREATE OR REPLACE FUNCTION public.open_external_boot_remote_module_attempt(
    p_peer_incarnation text,
    p_authority_id uuid,
    p_generation bigint,
    p_ack_sequence bigint,
    p_ack_digest text,
    p_operation_attempt_id uuid,
    p_operation_identity text,
    p_operation_digest text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_authority public.external_boot_authorities%ROWTYPE;
    v_head public.external_boot_authority_journal_heads%ROWTYPE;
    v_nonce text;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_provider_authority', 'member') THEN
        RAISE EXCEPTION 'provider authority is required' USING ERRCODE = '42501';
    END IF;
    IF (p_peer_incarnation IS NOT NULL
        AND octet_length(p_peer_incarnation) BETWEEN 1 AND 255
        AND p_authority_id IS NOT NULL
        AND p_generation IS NOT NULL AND p_generation > 0
        AND p_ack_sequence IS NOT NULL AND p_ack_sequence > 0
        AND p_ack_digest ~ '^sha256:[0-9a-f]{64}$'
        AND p_operation_attempt_id IS NOT NULL
        AND p_operation_identity ~ '^sha256:[0-9a-f]{64}$'
        AND p_operation_digest ~ '^sha256:[0-9a-f]{64}$') IS NOT TRUE THEN
        RAISE EXCEPTION 'remote module attempt authority facts are invalid'
            USING ERRCODE = '22023';
    END IF;
    v_nonce := replace(p_operation_attempt_id::text, '-', '');

    SELECT authority.* INTO v_authority
    FROM public.external_boot_authorities AS authority
    WHERE authority.id = p_authority_id AND authority.generation = p_generation;
    IF NOT FOUND THEN RETURN NULL; END IF;
    -- The worker retains this System lock while the authority-host operation runs.  The
    -- authority checkpoint serializes on the authority/job rows below instead of waiting on
    -- its caller and forming a distributed lock cycle (ADR-0618).
    PERFORM pg_advisory_xact_lock(
        pg_catalog.hashtextextended('kdive:worker-incarnation:' || p_peer_incarnation, 1803)
    );

    SELECT authority.* INTO v_authority
    FROM public.external_boot_authorities AS authority
    JOIN public.worker_incarnations AS worker
      ON worker.incarnation = authority.worker_incarnation
    JOIN public.external_boot_authority_acknowledgements AS acknowledgement
      ON acknowledgement.authority_id = authority.id
    JOIN public.jobs AS job
      ON job.id = authority.job_id AND job.attempt = authority.job_attempt
    WHERE authority.id = p_authority_id
      AND authority.generation = p_generation
      AND authority.worker_incarnation = p_peer_incarnation
      AND authority.state = 'current'
      AND authority.purpose = 'activate' AND authority.operation = 'activate'
      AND authority.provider_kind = 'remote-libvirt'
      AND worker.state = 'active' AND worker.fence_protocol = 4
      AND acknowledgement.journal_sequence = p_ack_sequence
      AND acknowledgement.journal_digest = p_ack_digest
      AND job.state = 'running' AND job.worker_id = p_peer_incarnation
      AND job.lease_expires_at > pg_catalog.clock_timestamp()
      AND NOT EXISTS (
          SELECT 1 FROM public.external_boot_authorities AS successor
          WHERE successor.system_id = authority.system_id
            AND successor.generation > authority.generation
            AND successor.state IN ('allocating', 'current')
      )
    FOR UPDATE OF authority, worker, job;
    IF NOT FOUND THEN RETURN NULL; END IF;

    SELECT head.* INTO v_head
    FROM public.external_boot_authority_journal_heads AS head
    WHERE head.system_id = v_authority.system_id
      AND head.authority_instance = v_authority.authority_instance
    FOR UPDATE;
    IF (v_head.authority_id = p_authority_id
        AND v_head.generation = p_generation
        AND v_head.phase = 'mutation-started'
        AND v_head.head_record->>'operation' = 'prepare'
        AND v_head.head_record->>'attempt_id' = p_operation_attempt_id::text
        AND v_head.head_record->>'operation_identity' = p_operation_identity
        AND v_head.head_record->>'operation_digest' = p_operation_digest
        AND EXISTS (
            SELECT 1 FROM public.external_boot_authority_audit AS audit
            WHERE audit.system_id = v_authority.system_id
              AND audit.activation_id = v_authority.activation_id
              AND audit.run_id = v_authority.run_id
              AND audit.plan_identity = v_authority.plan_identity
              AND audit.provider_kind = v_authority.provider_kind
              AND audit.authority_instance = v_authority.authority_instance
              AND audit.generation <= p_generation
              AND audit.operation = 'materialize'
              AND audit.outcome = 'result_committed'
        )
        AND EXISTS (
            SELECT 1 FROM public.external_boot_activations AS activation
            WHERE activation.id = v_authority.activation_id
              AND activation.system_id = v_authority.system_id
              AND activation.run_id = v_authority.run_id
              AND activation.plan_identity = v_authority.plan_identity
              AND activation.state = 'preparing'
              AND activation.materialization IS NOT NULL
              AND NOT activation.cleanup_complete
        )) IS NOT TRUE THEN
        RETURN NULL;
    END IF;

    IF EXISTS (
        SELECT 1 FROM public.remote_module_attempt_obligations AS obligation
        WHERE obligation.system_id = v_authority.system_id
          AND obligation.run_id = v_authority.run_id
          AND obligation.operation_nonce <> v_nonce
          AND (obligation.mutation_discharged_at IS NULL
               OR obligation.reap_discharged_at IS NULL)
    ) THEN
        RETURN NULL;
    END IF;
    INSERT INTO public.remote_module_attempt_obligations(system_id, run_id, operation_nonce)
    VALUES (v_authority.system_id, v_authority.run_id, v_nonce)
    ON CONFLICT (system_id, run_id, operation_nonce) DO NOTHING;
    IF NOT EXISTS (
        SELECT 1 FROM public.remote_module_attempt_obligations AS obligation
        WHERE obligation.system_id = v_authority.system_id
          AND obligation.run_id = v_authority.run_id
          AND obligation.operation_nonce = v_nonce
          AND obligation.mutation_discharged_at IS NULL
    ) THEN
        RETURN NULL;
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'module_attempt_obligation', pg_catalog.jsonb_build_object(
            'schema', 'module-attempt-obligation-receipt-v1',
            'system_id', v_authority.system_id::text,
            'run_id', v_authority.run_id::text,
            'operation_nonce', v_nonce
        )
    );
END
$$;
