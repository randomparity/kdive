-- Publish boot readiness with the fenced activation result, not afterward (ADR-0835).
DO $$
DECLARE
    v_function constant regprocedure :=
        'public.commit_external_boot_authority_result('
        'bytea,uuid,integer,uuid,bigint,uuid,uuid,uuid,text,text,text,text,text,text,'
        'bigint,text,text,jsonb)'::regprocedure;
    v_definition text := pg_get_functiondef(v_function);
    v_old constant text := $old$        UPDATE public.external_boot_activations
        SET state = 'active', terminal_evidence = v_evidence,
            activation_readiness_deadline = v_deadline
        WHERE id = p_activation_id;
    ELSIF v_operation IN ('recover', 'resolve-conflict') THEN$old$;
    v_new constant text := $new$        UPDATE public.external_boot_activations
        SET state = 'active', terminal_evidence = v_evidence,
            activation_readiness_deadline = v_deadline
        WHERE id = p_activation_id;
        INSERT INTO public.run_steps (run_id, step, state, result)
        VALUES (
            p_run_id, 'boot', 'succeeded',
            jsonb_build_object('system_id', p_system_id::text, 'boot_outcome', 'ready')
        )
        ON CONFLICT (run_id, step) DO NOTHING;
    ELSIF v_operation IN ('recover', 'resolve-conflict') THEN$new$;
BEGIN
    IF (length(v_definition) - length(replace(v_definition, v_old, ''))) / length(v_old) <> 1
    THEN
        RAISE EXCEPTION 'authority activation commit shape changed before boot readiness';
    END IF;
    EXECUTE replace(v_definition, v_old, v_new);
END
$$;
