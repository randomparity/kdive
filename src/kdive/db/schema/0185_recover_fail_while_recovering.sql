-- ADR-0714 amendment (#3429): a recover or resolve-conflict failure commits while recovering, and
-- a terminal one closes the recovery as 0181 does for release.
--
-- The resolve-conflict `fail` admission named only `recovery_conflict`, but the job's own
-- `recovery-attempt` commit moves the activation to `recovering`, so every later failure was
-- refused and the job stayed `running` until its lease lapsed.  The admission now names the states
-- allocation already admits for the purpose.  The release-only terminal close of 0181 now also
-- covers recover and resolve-conflict, which subsumes the recover readiness branch: that branch
-- refused a readiness failure that arrived before any recovery attempt existed.
-- The function is rewritten in place (as 0128, 0143, 0170 and 0181 do) so earlier patches survive.
DO $$
DECLARE
    v_commit constant regprocedure :=
        'public.commit_external_boot_authority_result(bytea,uuid,integer,uuid,bigint,uuid,uuid,uuid,text,text,text,text,text,text,bigint,text,text,jsonb)'::regprocedure;
    v_old_fail constant text := $old$           OR (p_purpose = 'resolve-conflict' AND (
               v_system.state NOT IN ('ready', 'crashed', 'failed')
               OR v_run.state IS DISTINCT FROM 'succeeded'
               OR v_activation.state IS DISTINCT FROM 'recovery_conflict'
           ))$old$;
    v_new_fail constant text := $new$           OR (p_purpose = 'resolve-conflict' AND (
               v_system.state NOT IN ('ready', 'crashed', 'failed')
               OR v_run.state IS DISTINCT FROM 'succeeded'
               OR v_activation.state NOT IN ('recovery_conflict', 'recovering')
           ))$new$;
    -- The recover readiness branch, removed: the widened terminal close below covers it, and its
    -- `superseded` return refused a readiness failure on an activation that never began recovery.
    v_old_readiness constant text := $old$        ELSIF p_purpose = 'recover'
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
            IF NOT FOUND THEN
                RETURN QUERY SELECT 'superseded'::text, NULL::text;
                RETURN;
            END IF;
            UPDATE public.remote_module_attempt_obligations
            SET mutation_discharged_at = now(), mutation_discharge_reason = 'terminal_escape'
            WHERE system_id = p_system_id AND mutation_discharged_at IS NULL;
        END IF;$old$;
    v_new_readiness constant text := $new$        END IF;$new$;
    v_old_close constant text := $old$        IF v_terminal AND p_purpose = 'release'
           AND v_activation.state = 'recovering' THEN$old$;
    v_new_close constant text := $new$        IF v_terminal
           AND p_purpose IN ('release', 'recover', 'resolve-conflict')
           AND v_activation.state = 'recovering' THEN$new$;
    v_definition text;
BEGIN
    v_definition := pg_get_functiondef(v_commit);
    IF (length(v_definition) - length(replace(v_definition, v_old_fail, ''))) / length(v_old_fail)
       <> 1 THEN
        RAISE EXCEPTION 'external boot resolve-conflict fail admission shape changed';
    END IF;
    IF (length(v_definition) - length(replace(v_definition, v_old_readiness, '')))
       / length(v_old_readiness) <> 1 THEN
        RAISE EXCEPTION 'external boot recover readiness failure shape changed';
    END IF;
    IF (length(v_definition) - length(replace(v_definition, v_old_close, '')))
       / length(v_old_close) <> 1 THEN
        RAISE EXCEPTION 'external boot terminal recovery close shape changed';
    END IF;
    EXECUTE replace(
        replace(
            replace(v_definition, v_old_fail, v_new_fail),
            v_old_readiness, v_new_readiness
        ),
        v_old_close, v_new_close
    );
END
$$;
