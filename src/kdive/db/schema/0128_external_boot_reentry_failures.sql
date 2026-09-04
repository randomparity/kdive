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
    v_old := $old$UPDATE public.external_boot_activations
            SET activation_readiness_deadline = v_deadline
            WHERE id = p_activation_id AND state IN ('activating', 'active');$old$;
    v_new := $new$UPDATE public.external_boot_activations
            SET activation_readiness_deadline = v_deadline
            WHERE id = p_activation_id AND state IN ('activating', 'active')
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
        RAISE EXCEPTION 'external boot activation deadline source shape changed';
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
    EXECUTE v_definition;
END
$$;

-- CREATE OR REPLACE FUNCTION public.commit_external_boot_authority_result(
--     signature retained by pg_get_functiondef above.
-- GRANT EXECUTE ON FUNCTION public.commit_external_boot_authority_result(
--         bytea, uuid, integer, uuid, bigint, uuid, uuid, uuid, text, text, text, text, text,
--         text, bigint, text, text, jsonb) TO kdive_worker;
