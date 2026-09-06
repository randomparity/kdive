-- Discharge mutation obligations in the authority-owned terminal transaction (#2172).
--
-- ``commit_external_boot_authority_result`` is SECURITY DEFINER and already holds the System
-- transaction advisory lock before it reaches either terminal edge.  Workers intentionally have
-- only SELECT on the obligations table, so compose the write into that function rather than
-- widening worker table privileges or splitting the terminal transition from its discharge.
DO $$
DECLARE
    v_function constant regprocedure :=
        'public.commit_external_boot_authority_result(bytea,uuid,integer,uuid,bigint,uuid,uuid,'
        'uuid,text,text,text,text,text,text,bigint,text,text,jsonb)'::regprocedure;
    v_definition text := pg_get_functiondef(v_function);
    v_old text;
    v_new text;
BEGIN
    v_old := $old$        UPDATE public.systems SET state = 'torn_down' WHERE id = p_system_id;
        UPDATE public.external_boot_activations$old$;
    v_new := $new$        UPDATE public.systems SET state = 'torn_down' WHERE id = p_system_id;
        UPDATE public.remote_module_attempt_obligations
        SET mutation_discharged_at = now(), mutation_discharge_reason = 'terminal_escape'
        WHERE system_id = p_system_id AND mutation_discharged_at IS NULL;
        UPDATE public.external_boot_activations$new$;
    IF position(v_old in v_definition) = 0 THEN
        RAISE EXCEPTION 'external boot teardown transition shape changed';
    END IF;
    v_definition := replace(v_definition, v_old, v_new);

    v_old := $old$            UPDATE public.external_boot_activations
            SET state = 'recovery_failed', terminal_evidence = v_evidence
            WHERE id = p_activation_id AND state = 'recovering';
        END IF;
        v_terminal := (p_result ->> 'terminal')::boolean OR v_job.attempt >= v_job.max_attempts;$old$;
    v_new := $new$            UPDATE public.external_boot_activations
            SET state = 'recovery_failed', terminal_evidence = v_evidence
            WHERE id = p_activation_id AND state = 'recovering';
            IF NOT FOUND THEN
                RETURN QUERY SELECT 'superseded'::text, NULL::text;
                RETURN;
            END IF;
            UPDATE public.remote_module_attempt_obligations
            SET mutation_discharged_at = now(), mutation_discharge_reason = 'terminal_escape'
            WHERE system_id = p_system_id AND mutation_discharged_at IS NULL;
        END IF;
        v_terminal := (p_result ->> 'terminal')::boolean OR v_job.attempt >= v_job.max_attempts;$new$;
    IF position(v_old in v_definition) = 0 THEN
        RAISE EXCEPTION 'external boot recovery failure transition shape changed';
    END IF;
    v_definition := replace(v_definition, v_old, v_new);

    EXECUTE v_definition;
END
$$;
