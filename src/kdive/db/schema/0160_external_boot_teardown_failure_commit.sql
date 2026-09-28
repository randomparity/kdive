-- ADR-0620: teardown admits the System in its real state (#2881).  Migration 0147 widened the
-- teardown allocation clause and the success finalizer but not the result-``fail`` precondition,
-- so every teardown failure for a System that is not ``failed`` returned ``superseded`` and its job
-- stayed running until lease expiry.  Mirror the 0147 allocation clause, keep the binding fence
-- untouched, and keep a teardown failure from writing the Run (teardown success never does, and
-- before this widening only Systems whose Run had already settled could reach that update).
DO $$
DECLARE
    v_definition text;
    v_old_precondition constant text := $old$           OR (p_purpose = 'teardown' AND (
               v_system.state IS DISTINCT FROM 'failed'
               OR v_activation.state NOT IN ('recovery_conflict', 'recovery_failed')
           ))
       )) THEN$old$;
    v_new_precondition constant text := $new$           OR (p_purpose = 'teardown' AND (
               v_system.state NOT IN (
                   'provisioning', 'ready', 'reprovisioning', 'restoring',
                   'paused', 'crashing', 'crashed', 'failed'
               )
               OR v_activation.state NOT IN (
                   'preparing', 'prepared', 'activating', 'active', 'recovering',
                   'recovered', 'recovery_conflict', 'recovery_failed', 'abandoned'
               )
               OR EXISTS (
                   SELECT 1 FROM public.external_boot_activations AS newer
                   WHERE newer.system_id = v_activation.system_id
                     AND (newer.created_at, newer.id) >
                         (v_activation.created_at, v_activation.id)
               )
           ))
       )) THEN$new$;
    v_old_run_update constant text := $old$            SET state = 'failed', failure_category = p_result ->> 'error_category'
            WHERE id = p_run_id AND state IN ('created', 'running');$old$;
    v_new_run_update constant text := $new$            SET state = 'failed', failure_category = p_result ->> 'error_category'
            WHERE id = p_run_id AND state IN ('created', 'running')
              AND p_purpose <> 'teardown';$new$;
BEGIN
    SELECT pg_get_functiondef((
        'public.commit_external_boot_authority_result('
        || 'bytea,uuid,integer,uuid,bigint,uuid,uuid,uuid,text,text,text,text,text,text,'
        || 'bigint,text,text,jsonb)'
    )::regprocedure) INTO v_definition;
    IF (length(v_definition) - length(replace(v_definition, v_old_precondition, '')))
       / length(v_old_precondition) <> 1 THEN
        RAISE EXCEPTION 'external boot teardown failure precondition shape changed';
    END IF;
    IF (length(v_definition) - length(replace(v_definition, v_old_run_update, '')))
       / length(v_old_run_update) <> 1 THEN
        RAISE EXCEPTION 'external boot terminal failure Run update shape changed';
    END IF;
    v_definition := replace(v_definition, v_old_precondition, v_new_precondition);
    v_definition := replace(v_definition, v_old_run_update, v_new_run_update);
    EXECUTE v_definition;
END
$$;
