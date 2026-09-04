-- Close external-boot re-entry failure and replay contracts (ADR-0595, #2202).
-- This preserves the existing signature, SECURITY DEFINER setting, search_path, and grants by
-- rebuilding the installed definition in place.
-- The rebuilt function remains SECURITY DEFINER with SET search_path = ''.
DO $$
DECLARE
    v_function constant regprocedure :=
        'public.commit_external_boot_authority_result(bytea,uuid,integer,uuid,bigint,uuid,uuid,'
        'uuid,text,text,text,text,text,text,bigint,text,text,jsonb)'::regprocedure;
    v_definition text := pg_get_functiondef(v_function);
    v_old text;
    v_new text;
BEGIN
    v_old := $old$               'schema', 'operation', 'error_category', 'failure_context', 'terminal'
           ])$old$;
    v_new := $new$               'schema', 'operation', 'error_category', 'failure_context', 'terminal',
               'recovery_readiness_deadline'
           ])$new$;
    IF position(v_old in v_definition) = 0 THEN
        RAISE EXCEPTION 'external boot failure result field shape changed';
    END IF;
    v_definition := replace(v_definition, v_old, v_new);

    v_old := $old$           OR NOT (p_result ? 'failure_context')$old$;
    v_new := $new$           OR (
               p_result ->> 'error_category' = 'boot_timeout'
               AND (p_result ->> 'terminal')::boolean
               AND (
                   NOT (p_result ? 'recovery_readiness_deadline')
                   OR jsonb_typeof(p_result -> 'recovery_readiness_deadline')
                      IS DISTINCT FROM 'string'
               )
           )
           OR (
               NOT (p_result ->> 'error_category' = 'boot_timeout'
                   AND (p_result ->> 'terminal')::boolean)
               AND p_result ? 'recovery_readiness_deadline'
           )
           OR NOT (p_result ? 'failure_context')$new$;
    IF position(v_old in v_definition) = 0 THEN
        RAISE EXCEPTION 'external boot failure recovery deadline shape changed';
    END IF;
    v_definition := replace(v_definition, v_old, v_new);

    v_old := $old$IF v_operation <> 'fail' AND v_operation <> p_admitted_operation THEN$old$;
    v_new := $new$IF v_operation <> 'fail' AND v_operation <> p_admitted_operation
       AND NOT (
           (p_admitted_operation = 'activate' AND v_operation = 'deadline')
           OR (p_admitted_operation = 'recover'
               AND v_operation IN ('deadline', 'recovery-attempt'))
           OR (p_admitted_operation = 'resolve-conflict'
               AND v_operation = 'recovery-attempt')
       ) THEN$new$;
    IF position(v_old in v_definition) = 0 THEN
        RAISE EXCEPTION 'external boot intermediate operation shape changed';
    END IF;
    v_definition := replace(v_definition, v_old, v_new);

    v_old := $old$           OR v_attempt.authority_generation <> p_generation$old$;
    v_new := $new$           OR v_attempt.authority_generation > p_generation$new$;
    IF position(v_old in v_definition) = 0 THEN
        RAISE EXCEPTION 'external boot recovery attempt generation shape changed';
    END IF;
    v_definition := replace(v_definition, v_old, v_new);

    v_old := $old$OR (v_operation = 'resolve-conflict' AND (
           v_system.state NOT IN ('ready', 'crashed', 'failed')
           OR v_run.state IS DISTINCT FROM 'succeeded'
           OR v_activation.state IS DISTINCT FROM 'recovery_conflict'
           OR v_attempt.state IS DISTINCT FROM 'conflict'
       ))$old$;
    v_new := $new$OR (v_operation = 'resolve-conflict' AND (
           v_system.state NOT IN ('ready', 'crashed', 'failed')
           OR v_run.state IS DISTINCT FROM 'succeeded'
           OR v_activation.state IS DISTINCT FROM 'recovering'
           OR v_attempt.state IS DISTINCT FROM 'recovering'
       ))$new$;
    IF position(v_old in v_definition) = 0 THEN
        RAISE EXCEPTION 'external boot resolve-conflict commit shape changed';
    END IF;
    v_definition := replace(v_definition, v_old, v_new);

    v_old := $old$        v_terminal := (p_result ->> 'terminal')::boolean OR v_job.attempt >= v_job.max_attempts;$old$;
    v_new := $new$        IF p_purpose = 'activate'
           AND p_result ->> 'error_category' = 'boot_timeout'
           AND (p_result ->> 'terminal')::boolean THEN
            INSERT INTO public.external_boot_recovery_attempts (
                activation_id, attempt_number, attempt_id, authority_generation,
                recovery_basis, recovery_readiness_deadline, state
            ) VALUES (
                p_activation_id,
                coalesce((
                    SELECT max(ra.attempt_number) + 1
                    FROM public.external_boot_recovery_attempts AS ra
                    WHERE ra.activation_id = p_activation_id
                ), 1),
                p_authority_id, p_generation, 'recovery_point',
                (p_result ->> 'recovery_readiness_deadline')::timestamptz, 'recovering'
            ) ON CONFLICT (attempt_id) DO NOTHING;
            UPDATE public.external_boot_activations
            SET state = 'recovering', current_attempt_id = p_authority_id
            WHERE id = p_activation_id AND state = 'activating';
            IF NOT FOUND THEN
                RETURN QUERY SELECT 'superseded'::text, NULL::text;
                RETURN;
            END IF;
        ELSIF p_purpose = 'recover'
           AND p_result ->> 'error_category' = 'readiness_failure'
           AND (p_result ->> 'terminal')::boolean THEN
            v_evidence := jsonb_build_object(
                'schema', 'external-boot-terminal-evidence-v1',
                'activation_id', p_activation_id::text,
                'system_id', p_system_id::text,
                'outcome', 'recovery_failed',
                'composite_state', v_ack.positive_quiescence_digest,
                'objects', '[]'::jsonb,
                'observed_at', to_jsonb(clock_timestamp())
            );
            UPDATE public.external_boot_recovery_attempts
            SET state = 'failed', terminal_evidence = v_evidence,
                conflict_evidence = NULL
            WHERE activation_id = p_activation_id
              AND attempt_id = v_activation.current_attempt_id
              AND state = 'recovering';
            IF NOT FOUND THEN
                RETURN QUERY SELECT 'superseded'::text, NULL::text;
                RETURN;
            END IF;
            UPDATE public.external_boot_activations
            SET state = 'recovery_failed', terminal_evidence = v_evidence
            WHERE id = p_activation_id AND state = 'recovering';
        END IF;
        v_terminal := (p_result ->> 'terminal')::boolean OR v_job.attempt >= v_job.max_attempts;$new$;
    IF position(v_old in v_definition) = 0 THEN
        RAISE EXCEPTION 'external boot recovery expiry shape changed';
    END IF;
    v_definition := replace(v_definition, v_old, v_new);

    v_old := $old$OR v_activation.state IS DISTINCT FROM 'activating'$old$;
    v_new := $new$OR v_activation.state NOT IN ('prepared', 'activating')$new$;
    IF position(v_old in v_definition) = 0 THEN
        RAISE EXCEPTION 'external boot activation deadline admission shape changed';
    END IF;
    v_definition := replace(v_definition, v_old, v_new);

    v_old := $old$UPDATE public.external_boot_activations
            SET activation_readiness_deadline = v_deadline
            WHERE id = p_activation_id AND state IN ('activating', 'active');$old$;
    v_new := $new$UPDATE public.external_boot_activations
            SET state = CASE WHEN state = 'prepared' THEN 'activating' ELSE state END,
                activation_readiness_deadline = v_deadline
            WHERE id = p_activation_id AND state IN ('prepared', 'activating', 'active')
              AND (activation_readiness_deadline IS NULL
                   OR activation_readiness_deadline = v_deadline);
            IF NOT FOUND OR EXISTS (
                SELECT 1 FROM public.external_boot_activations
                WHERE id = p_activation_id
                  AND activation_readiness_deadline <> v_deadline
            ) THEN
                RETURN QUERY SELECT 'superseded'::text, NULL::text;
                RETURN;
            END IF;$new$;
    IF position(v_old in v_definition) = 0 THEN
        RAISE EXCEPTION 'external boot activation deadline transition shape changed';
    END IF;
    v_definition := replace(v_definition, v_old, v_new);

    v_old := $old$UPDATE public.external_boot_recovery_attempts
            SET recovery_readiness_deadline = v_deadline
            WHERE activation_id = p_activation_id
              AND attempt_id = v_activation.current_attempt_id
              AND state = 'recovering';$old$;
    v_new := $new$UPDATE public.external_boot_recovery_attempts
            SET recovery_readiness_deadline = v_deadline
            WHERE activation_id = p_activation_id
              AND attempt_id = v_activation.current_attempt_id
              AND state = 'recovering'
              AND (recovery_readiness_deadline IS NULL
                   OR recovery_readiness_deadline = v_deadline);
            IF NOT FOUND OR EXISTS (
                SELECT 1 FROM public.external_boot_recovery_attempts
                WHERE activation_id = p_activation_id
                  AND attempt_id = v_activation.current_attempt_id
                  AND recovery_readiness_deadline <> v_deadline
            ) THEN
                RETURN QUERY SELECT 'superseded'::text, NULL::text;
                RETURN;
            END IF;$new$;
    IF position(v_old in v_definition) = 0 THEN
        RAISE EXCEPTION 'external boot recovery deadline source shape changed';
    END IF;
    v_definition := replace(v_definition, v_old, v_new);

    v_old := $old$            );
        EXCEPTION
            WHEN invalid_datetime_format OR datetime_field_overflow
                OR invalid_text_representation OR invalid_parameter_value THEN$old$;
    v_new := $new$            ) ON CONFLICT (attempt_id) DO NOTHING;
            IF NOT FOUND AND NOT EXISTS (
                SELECT 1 FROM public.external_boot_recovery_attempts
                WHERE activation_id = p_activation_id
                  AND attempt_id = (p_result ->> 'attempt_id')::uuid
                  AND authority_generation = p_generation
                  AND recovery_basis = p_result ->> 'recovery_basis'
                  AND recovery_readiness_deadline = v_deadline
                  AND state = 'recovering'
            ) THEN
                RETURN QUERY SELECT 'superseded'::text, NULL::text;
                RETURN;
            END IF;
        EXCEPTION
            WHEN invalid_datetime_format OR datetime_field_overflow
                OR invalid_text_representation OR invalid_parameter_value THEN$new$;
    IF position(v_old in v_definition) = 0 THEN
        RAISE EXCEPTION 'external boot recovery-attempt source shape changed';
    END IF;
    v_definition := replace(v_definition, v_old, v_new);

    v_old := $old$OR (v_operation = 'recovery-attempt' AND (
           v_system.state NOT IN ('ready', 'crashed')
           OR v_run.state IS DISTINCT FROM 'succeeded'
           OR v_activation.state IS DISTINCT FROM 'active'
       ))$old$;
    v_new := $new$OR (v_operation = 'recovery-attempt' AND (
           v_system.state NOT IN ('ready', 'crashed', 'failed')
           OR v_run.state IS DISTINCT FROM 'succeeded'
           OR (p_purpose = 'recover' AND v_activation.state IS DISTINCT FROM 'active')
           OR (p_purpose = 'resolve-conflict' AND (
               v_activation.state IS DISTINCT FROM 'recovery_conflict'
               OR v_attempt.state IS DISTINCT FROM 'conflict'
           ))
       ))$new$;
    IF position(v_old in v_definition) = 0 THEN
        RAISE EXCEPTION 'external boot recovery-attempt admission shape changed';
    END IF;
    v_definition := replace(v_definition, v_old, v_new);

    v_old := $old$WHERE field <> 'phase'$old$;
    v_new := $new$WHERE field NOT IN ('phase', 'reason', 'next_action')$new$;
    IF position(v_old in v_definition) = 0 THEN
        RAISE EXCEPTION 'external boot failure context source shape changed';
    END IF;
    v_definition := replace(v_definition, v_old, v_new);

    v_old := $old$           OR (
               v_failure_context ? 'phase'$old$;
    v_new := $new$           OR NOT (
               NOT (v_failure_context ? 'reason') AND NOT (v_failure_context ? 'next_action')
               OR (v_failure_context ->> 'reason' = 'observed_identity_stale'
                   AND v_failure_context ->> 'next_action' = 'systems.get'
                   AND p_result ->> 'error_category' = 'stale_handle'
                   AND (p_result ->> 'terminal')::boolean = true)
               OR (v_failure_context ->> 'reason' = 'reservation_not_ready'
                   AND v_failure_context ->> 'next_action' = 'jobs.wait'
                   AND p_result ->> 'error_category' = 'infrastructure_failure'
                   AND (p_result ->> 'terminal')::boolean = false)
               OR (v_failure_context ->> 'reason' = 'authority_superseded'
                   AND v_failure_context ->> 'next_action' = 'jobs.get'
                   AND p_result ->> 'error_category' = 'stale_handle'
                   AND (p_result ->> 'terminal')::boolean = true)
           )
           OR (
               v_failure_context ? 'phase'$new$;
    IF position(v_old in v_definition) = 0 THEN
        RAISE EXCEPTION 'external boot failure validation source shape changed';
    END IF;
    v_definition := replace(v_definition, v_old, v_new);

    v_old := $old$           OR NOT EXISTS (
               SELECT 1 FROM public.external_boot_reservations AS reservation
               WHERE reservation.activation_id = p_activation_id
                 AND reservation.state = 'ready'
                 AND reservation.store_identity = v_evidence #>> '{store_identity,ref}'
                 AND reservation.owner_key = v_evidence #>> '{owner_key,ref}'
                 AND reservation.reserved_bytes::text = v_evidence ->> 'reserved_bytes'
           ) THEN
            RAISE EXCEPTION 'external boot release evidence is invalid'
                USING ERRCODE = '22023';
        END IF;
        INSERT INTO public.external_boot_reservation_releases ($old$;
    v_new := $new$           THEN
            RAISE EXCEPTION 'external boot release evidence is invalid'
                USING ERRCODE = '22023';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM public.external_boot_reservations AS reservation
            WHERE reservation.activation_id = p_activation_id
              AND reservation.state = 'ready'
              AND reservation.store_identity = v_evidence #>> '{store_identity,ref}'
              AND reservation.owner_key = v_evidence #>> '{owner_key,ref}'
              AND reservation.reserved_bytes::text = v_evidence ->> 'reserved_bytes'
            FOR UPDATE
        ) THEN
            RETURN QUERY SELECT 'reservation_not_ready'::text, NULL::text;
            RETURN;
        END IF;
        INSERT INTO public.external_boot_reservation_releases ($new$;
    IF position(v_old in v_definition) = 0 THEN
        RAISE EXCEPTION 'external boot release reservation classifier shape changed';
    END IF;
    v_definition := replace(v_definition, v_old, v_new);

    v_old := $old$    v_marker := v_job.payload -> 'external_boot_authority_v1';

    IF v_incarnation IS NULL OR v_system.id IS NULL OR v_run.id IS NULL$old$;
    v_new := $new$    v_marker := v_job.payload -> 'external_boot_authority_v1';

    -- A lost job attempt is not an activation verdict.  Keep the historical status so the
    -- worker drops a result whose lease was genuinely reclaimed.
    IF v_incarnation IS NULL OR v_job.id IS NULL
       OR v_job.state <> 'running' OR v_job.worker_id <> v_incarnation
       OR v_job.attempt <> p_attempt THEN
        RETURN QUERY SELECT 'superseded'::text, NULL::text;
        RETURN;
    END IF;

    IF v_activation.id IS NULL OR v_system.id IS NULL OR v_run.id IS NULL
       OR v_activation.system_id <> p_system_id OR v_activation.run_id <> p_run_id
       OR v_activation.plan_identity <> p_plan_identity
       OR v_system.id <> p_system_id
       OR v_run.id <> p_run_id OR v_run.system_id <> p_system_id THEN
        UPDATE public.jobs
        SET state = 'failed', error_category = 'stale_handle',
            failure_context = jsonb_build_object(
                'phase', 'commit', 'reason', 'observed_identity_stale',
                'next_action', 'systems.get'
            )
        WHERE id = p_job_id AND state = 'running';
        UPDATE public.runs
        SET state = 'failed', failure_category = 'stale_handle'
        WHERE id = p_run_id AND state IN ('created', 'running');
        UPDATE public.external_boot_authorities
        SET state = 'retired', retired_at = clock_timestamp()
        WHERE id = p_authority_id AND state = 'current';
        IF v_activation.id IS NOT NULL AND v_system.id IS NOT NULL
           AND v_run.id IS NOT NULL THEN
        INSERT INTO public.external_boot_authority_audit (
            authority_id, system_id, allocation_id, activation_id, run_id, plan_identity,
            job_id, job_attempt, worker_incarnation, generation, purpose, provider_kind,
            authority_instance, operation, operation_identity, operation_digest,
            journal_sequence, journal_digest, outcome
        ) VALUES (
            p_authority_id, p_system_id, v_authority.allocation_id, p_activation_id,
            p_run_id, v_activation.plan_identity, p_job_id, p_attempt, v_incarnation, p_generation,
            p_purpose, p_provider_kind, p_authority_instance, 'fail', p_operation_identity,
            p_operation_digest, p_journal_sequence, p_journal_digest,
            'result_failed'
        );
        END IF;
        RETURN QUERY SELECT 'observed_identity_stale'::text, 'failed'::text;
        RETURN;
    END IF;

    IF v_incarnation IS NULL OR v_system.id IS NULL OR v_run.id IS NULL$new$;
    IF position(v_old in v_definition) = 0 THEN
        RAISE EXCEPTION 'external boot losing result classifier insertion shape changed';
    END IF;
    v_definition := replace(v_definition, v_old, v_new);

    v_old := $old$       ) THEN
        RETURN QUERY SELECT 'superseded'::text, NULL::text;
        RETURN;
    END IF;

    WITH RECURSIVE result_nodes(value) AS ($old$;
    v_new := $new$       ) THEN
        UPDATE public.jobs
        SET state = 'failed', error_category = 'stale_handle',
            failure_context = jsonb_build_object(
                'phase', 'commit', 'reason', 'authority_superseded',
                'next_action', 'jobs.get'
            )
        WHERE id = p_job_id AND state = 'running';
        UPDATE public.runs
        SET state = 'failed', failure_category = 'stale_handle'
        WHERE id = p_run_id AND state IN ('created', 'running');
        UPDATE public.external_boot_authorities
        SET state = 'retired', retired_at = clock_timestamp()
        WHERE id = p_authority_id AND state = 'current';
        INSERT INTO public.external_boot_authority_audit (
            authority_id, system_id, allocation_id, activation_id, run_id, plan_identity,
            job_id, job_attempt, worker_incarnation, generation, purpose, provider_kind,
            authority_instance, operation, operation_identity, operation_digest,
            journal_sequence, journal_digest, outcome
        ) VALUES (
            p_authority_id, p_system_id, v_authority.allocation_id, p_activation_id,
            p_run_id, p_plan_identity, p_job_id, p_attempt, v_incarnation, p_generation,
            p_purpose, p_provider_kind, p_authority_instance, 'fail', p_operation_identity,
            p_operation_digest, p_journal_sequence, p_journal_digest,
            'result_failed'
        );
        RETURN QUERY SELECT 'authority_superseded'::text, 'failed'::text;
        RETURN;
    END IF;

    WITH RECURSIVE result_nodes(value) AS ($new$;
    IF position(v_old in v_definition) = 0 THEN
        RAISE EXCEPTION 'external boot losing result classifier return shape changed';
    END IF;
    v_definition := replace(v_definition, v_old, v_new);
    EXECUTE v_definition;
END
$$;

DO $$
DECLARE
    v_function constant regprocedure :=
        'public.allocate_external_boot_authority(bytea,uuid,integer,uuid,uuid,uuid,text,text,text,text,text)'::regprocedure;
    v_definition text := pg_get_functiondef(v_function);
    v_old text;
    v_new text;
BEGIN
    v_old := $old$OR (p_purpose = 'resolve-conflict'
           AND v_operation NOT IN ('resolve-conflict', 'fail'))$old$;
    v_new := $new$OR (p_purpose = 'resolve-conflict'
           AND v_operation NOT IN ('resolve-conflict', 'recovery-attempt', 'fail'))$new$;
    IF position(v_old in v_definition) = 0 THEN
        RAISE EXCEPTION 'external boot resolve-conflict allocation operation shape changed';
    END IF;
    v_definition := replace(v_definition, v_old, v_new);

    v_old := $old$OR (p_purpose = 'resolve-conflict' AND (
           v_system.state NOT IN ('ready', 'crashed', 'failed')
           OR v_run.state <> 'succeeded'
           OR v_activation.state <> 'recovery_conflict'
       ))$old$;
    v_new := $new$OR (p_purpose = 'resolve-conflict' AND (
           v_system.state NOT IN ('ready', 'crashed', 'failed')
           OR v_run.state <> 'succeeded'
           OR v_activation.state NOT IN ('recovery_conflict', 'recovering')
       ))$new$;
    IF position(v_old in v_definition) = 0 THEN
        RAISE EXCEPTION 'external boot resolve-conflict allocation state shape changed';
    END IF;
    EXECUTE replace(v_definition, v_old, v_new);
END
$$;

-- CREATE OR REPLACE FUNCTION public.commit_external_boot_authority_result(
--     signature retained by pg_get_functiondef above.
-- GRANT EXECUTE ON FUNCTION public.commit_external_boot_authority_result(
--         bytea, uuid, integer, uuid, bigint, uuid, uuid, uuid, text, text, text, text, text,
--         text, bigint, text, text, jsonb) TO kdive_worker;
