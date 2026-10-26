-- 0178_external_boot_conflict_from_head.sql — ADR-0805: a worker records recovery_conflict
-- only from the authority's own terminal conflict journal head.

CREATE FUNCTION public.record_external_boot_conflict_from_head(
    p_credential_hash bytea, p_job_id uuid, p_attempt integer,
    p_authority_id uuid, p_generation bigint, p_evidence jsonb
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_authority public.external_boot_authorities%ROWTYPE;
    v_job public.jobs%ROWTYPE;
    v_activation public.external_boot_activations%ROWTYPE;
    v_attempt public.external_boot_recovery_attempts%ROWTYPE;
    v_head public.external_boot_authority_journal_heads%ROWTYPE;
    v_operation text;
    v_identity text;
    v_digest text;
    v_incarnation text;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    IF jsonb_typeof(p_evidence) IS DISTINCT FROM 'object'
       OR pg_column_size(p_evidence) > 65536
       OR jsonb_typeof(p_evidence -> 'objects') IS DISTINCT FROM 'array'
       OR jsonb_typeof(p_evidence -> 'observed_at') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'external boot conflict evidence is invalid' USING ERRCODE = '22023';
    END IF;
    SELECT incarnation INTO v_incarnation FROM public.worker_incarnations
    WHERE credential_hash = p_credential_hash AND state = 'active' AND fence_protocol = 4;
    SELECT * INTO v_authority FROM public.external_boot_authorities
    WHERE id = p_authority_id AND generation = p_generation;
    IF v_incarnation IS NULL OR v_authority.id IS NULL THEN RETURN 'superseded'; END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:system:' || v_authority.system_id::text, 2125));
    SELECT * INTO v_authority FROM public.external_boot_authorities
    WHERE id = p_authority_id AND generation = p_generation FOR UPDATE;
    SELECT * INTO v_job FROM public.jobs WHERE id = p_job_id FOR UPDATE;
    SELECT * INTO v_activation FROM public.external_boot_activations
    WHERE id = v_authority.activation_id FOR UPDATE;
    SELECT * INTO v_attempt FROM public.external_boot_recovery_attempts
    WHERE activation_id = v_activation.id AND attempt_id = v_activation.current_attempt_id
    FOR UPDATE;
    SELECT * INTO v_head FROM public.external_boot_authority_journal_heads
    WHERE system_id = v_authority.system_id
      AND authority_instance = v_authority.authority_instance
    FOR UPDATE;
    -- A release records the conflict its derived recover phase journaled (ADR-0614).
    IF v_authority.purpose = 'release' THEN
        v_operation := 'recover';
        SELECT bound.operation_identity, bound.operation_digest INTO v_identity, v_digest
        FROM public.derive_external_boot_release_phase_binding(jsonb_build_object(
            'authority_id', v_authority.id, 'generation', v_authority.generation,
            'system_id', v_authority.system_id, 'activation_id', v_authority.activation_id,
            'run_id', v_authority.run_id, 'plan_identity', v_authority.plan_identity,
            'provider_kind', v_authority.provider_kind,
            'authority_instance', v_authority.authority_instance,
            'worker_incarnation', v_authority.worker_incarnation,
            'root_operation_identity', v_authority.operation_identity,
            'root_operation_digest', v_authority.operation_digest), 'recover') AS bound;
    ELSE
        v_operation := v_authority.operation;
        v_identity := v_authority.operation_identity;
        v_digest := v_authority.operation_digest;
    END IF;
    IF (v_authority.state = 'current'
        AND v_authority.purpose IN ('activate', 'recover', 'resolve-conflict', 'release')
        AND v_authority.operation = v_authority.purpose
        AND v_authority.worker_incarnation = v_incarnation
        AND v_authority.job_id = p_job_id AND v_authority.job_attempt = p_attempt
        AND v_job.state = 'running' AND v_job.worker_id = v_incarnation
        AND v_job.attempt = p_attempt AND v_job.lease_expires_at > clock_timestamp()
        AND (
            v_activation.state = 'activating'
            OR (v_activation.state = 'recovering' AND v_attempt.state = 'recovering'
                AND v_attempt.authority_generation <= p_generation
                AND v_head.head_record ->> 'attempt_id' = v_attempt.attempt_id::text)
        )
        AND v_head.authority_id = p_authority_id AND v_head.generation = p_generation
        AND v_head.phase = 'terminal'
        AND v_head.head_record ->> 'operation' = v_operation
        AND v_head.head_record ->> 'operation_identity' = v_identity
        AND v_head.head_record ->> 'operation_digest' = v_digest
        AND v_head.head_record ->> 'activation_id' = v_activation.id::text
        AND v_head.head_record ->> 'outcome' = 'conflict'
        AND v_head.head_record #>> '{observation,category}' IN ('conflict', 'mixed')
        AND p_evidence = jsonb_build_object(
            'schema', 'external-boot-conflict-evidence-v1',
            'activation_id', v_activation.id::text,
            'system_id', v_activation.system_id::text,
            'observation_id', v_head.head_record #>> '{observation,observation_id}',
            'composite_state', v_head.head_record #>> '{observation,composite_state}',
            'objects', p_evidence -> 'objects',
            'observed_at', p_evidence -> 'observed_at')
        -- Every object is a reference the activation's own evidence already names.
        AND NOT EXISTS (
            SELECT 1 FROM jsonb_array_elements(p_evidence -> 'objects') AS item(value)
            WHERE jsonb_typeof(item.value) IS DISTINCT FROM 'object'
               OR item.value - 'ref' <> '{}'::jsonb
               OR jsonb_typeof(item.value -> 'ref') IS DISTINCT FROM 'string'
               OR NOT (item.value ->> 'ref' = ANY (array_remove(ARRAY[
                   v_activation.materialization #>> '{artifacts,kernel,ref}',
                   v_activation.materialization #>> '{artifacts,modules,ref}',
                   v_activation.materialization #>> '{artifacts,initrd,ref}',
                   v_activation.recovery_point #>> '{recovery_ref,ref}'
               ], NULL)))
        )
    ) IS NOT TRUE THEN
        RETURN 'superseded';
    END IF;

    IF v_activation.state = 'activating' THEN
        INSERT INTO public.external_boot_recovery_attempts (
            activation_id, attempt_number, attempt_id, authority_generation, recovery_basis,
            state, conflict_evidence
        ) VALUES (
            v_activation.id,
            coalesce((SELECT max(attempt_number) + 1 FROM public.external_boot_recovery_attempts
                      WHERE activation_id = v_activation.id), 1),
            (v_head.head_record ->> 'attempt_id')::uuid, p_generation, 'recovery_point',
            'conflict', p_evidence
        );
        UPDATE public.external_boot_activations
        SET state = 'recovery_conflict',
            current_attempt_id = (v_head.head_record ->> 'attempt_id')::uuid
        WHERE id = v_activation.id;
    ELSE
        UPDATE public.external_boot_recovery_attempts
        SET state = 'conflict', conflict_evidence = p_evidence,
            recovery_readiness_deadline = NULL, authority_generation = p_generation
        WHERE activation_id = v_activation.id AND attempt_id = v_attempt.attempt_id;
        UPDATE public.external_boot_activations SET state = 'recovery_conflict'
        WHERE id = v_activation.id;
    END IF;
    UPDATE public.jobs
    SET state = 'failed', error_category = 'conflict',
        failure_context = jsonb_build_object('phase', 'observation')
    WHERE id = p_job_id;
    UPDATE public.external_boot_authorities
    SET state = 'retired', retired_at = clock_timestamp()
    WHERE id = p_authority_id;
    INSERT INTO public.external_boot_authority_audit (
        authority_id, system_id, allocation_id, activation_id, run_id, plan_identity, job_id,
        job_attempt, worker_incarnation, generation, purpose, provider_kind,
        authority_instance, operation, operation_identity, operation_digest,
        journal_sequence, journal_digest, outcome
    ) VALUES (
        p_authority_id, v_authority.system_id, v_authority.allocation_id, v_activation.id,
        v_authority.run_id, v_authority.plan_identity, p_job_id, p_attempt, v_incarnation,
        p_generation, v_authority.purpose, v_authority.provider_kind,
        v_authority.authority_instance, v_authority.operation, v_authority.operation_identity,
        v_authority.operation_digest, v_head.sequence, v_head.digest, 'result_failed'
    );
    RETURN 'applied';
END $$;

REVOKE ALL ON FUNCTION public.record_external_boot_conflict_from_head(
    bytea,uuid,integer,uuid,bigint,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.record_external_boot_conflict_from_head(
    bytea,uuid,integer,uuid,bigint,jsonb) TO kdive_worker;
