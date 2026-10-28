-- ADR-0714 amendment (#3396): a release attempt that fails after its derived recovery began
-- commits its failure, and the next attempt of the same job adopts the in-flight recovery.
--
-- 0143 moves a release's activation to `recovering` before the module restore and the provider
-- RECOVER, but the release `fail` admission (0122) names no `recovering` state, so every failure
-- after that point was refused: the job stayed `running` until its lease lapsed, on every
-- attempt.  The failure commit retires the failing authority, as it does for every purpose, so
-- allocation and the begin re-entry join also accept a retired prior authority of the same job.
-- A terminal release failure that finds its recovery still in flight closes the attempt as failed
-- and the activation as `recovery_failed`, mirroring the recover readiness branch, so a release
-- that gave up no longer leaves a recovery that nothing can close.
-- Each function is rewritten in place (as 0128, 0143 and 0170 do) so earlier patches survive.
DO $$
DECLARE
    v_commit constant regprocedure :=
        'public.commit_external_boot_authority_result(bytea,uuid,integer,uuid,bigint,uuid,uuid,uuid,text,text,text,text,text,text,bigint,text,text,jsonb)'::regprocedure;
    v_allocate constant regprocedure :=
        'public.allocate_external_boot_authority(bytea,uuid,integer,uuid,uuid,uuid,text,text,text,text,text)'::regprocedure;
    v_begin constant regprocedure :=
        'public.begin_external_boot_derived_release_recovery(bytea,uuid,integer,uuid,bigint,uuid,timestamptz)'::regprocedure;
    -- The release arm of the `fail` admission.  A recovering activation is admitted only when its
    -- current recovery attempt is the failing authority's own.
    v_old_fail constant text := $old$           OR (p_purpose = 'release' AND (
               v_run.state IS DISTINCT FROM 'succeeded'
               OR v_activation.state NOT IN (
                   'active', 'recovered', 'abandoned',
                   'recovery_conflict', 'recovery_failed'
               )
           ))$old$;
    v_new_fail constant text := $new$           OR (p_purpose = 'release' AND (
               v_run.state IS DISTINCT FROM 'succeeded'
               OR v_activation.state NOT IN (
                   'active', 'recovered', 'abandoned',
                   'recovery_conflict', 'recovery_failed', 'recovering'
               )
               OR (v_activation.state = 'recovering'
                   AND v_attempt.authority_generation IS DISTINCT FROM p_generation)
           ))$new$;
    -- The terminal arm of the `fail` branch: a release that ends here has abandoned its recovery.
    v_old_close constant text := $old$        IF v_terminal THEN
            UPDATE public.runs$old$;
    v_new_close constant text := $new$        IF v_terminal AND p_purpose = 'release'
           AND v_activation.state = 'recovering' THEN
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
            SET state = 'failed', terminal_evidence = v_evidence, conflict_evidence = NULL
            WHERE activation_id = p_activation_id
              AND attempt_id = v_activation.current_attempt_id
              AND state = 'recovering';
            UPDATE public.external_boot_activations
            SET state = 'recovery_failed', terminal_evidence = v_evidence
            WHERE id = p_activation_id AND state = 'recovering';
            UPDATE public.remote_module_attempt_obligations
            SET mutation_discharged_at = now(), mutation_discharge_reason = 'terminal_escape'
            WHERE system_id = p_system_id AND mutation_discharged_at IS NULL;
        END IF;
        IF v_terminal THEN
            UPDATE public.runs$new$;
    v_old_allocate constant text := $old$prior.state = 'current'$old$;
    v_new_allocate constant text := $new$prior.state IN ('current', 'retired')$new$;
    v_old_begin constant text := $old$prior.state = 'superseded'$old$;
    v_new_begin constant text := $new$prior.state IN ('superseded', 'retired')$new$;
    v_definition text;
BEGIN
    v_definition := pg_get_functiondef(v_commit);
    IF (length(v_definition) - length(replace(v_definition, v_old_fail, ''))) / length(v_old_fail)
       <> 1 THEN
        RAISE EXCEPTION 'external boot release fail admission shape changed';
    END IF;
    IF (length(v_definition) - length(replace(v_definition, v_old_close, '')))
       / length(v_old_close) <> 1 THEN
        RAISE EXCEPTION 'external boot terminal failure shape changed';
    END IF;
    EXECUTE replace(replace(v_definition, v_old_fail, v_new_fail), v_old_close, v_new_close);

    v_definition := pg_get_functiondef(v_allocate);
    IF (length(v_definition) - length(replace(v_definition, v_old_allocate, '')))
       / length(v_old_allocate) <> 1 THEN
        RAISE EXCEPTION 'external boot release allocation adoption shape changed';
    END IF;
    EXECUTE replace(v_definition, v_old_allocate, v_new_allocate);

    v_definition := pg_get_functiondef(v_begin);
    IF (length(v_definition) - length(replace(v_definition, v_old_begin, '')))
       / length(v_old_begin) <> 1 THEN
        RAISE EXCEPTION 'external boot derived release begin adoption shape changed';
    END IF;
    EXECUTE replace(v_definition, v_old_begin, v_new_begin);
END
$$;
