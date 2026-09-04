-- Admit bounded, versioned command-line mismatch diagnostics (#2175).
DO $$
DECLARE
    v_function constant regprocedure :=
        'public.commit_external_boot_authority_result(bytea,uuid,integer,uuid,bigint,uuid,uuid,uuid,text,text,text,text,text,text,bigint,text,text,jsonb)'::regprocedure;
    v_definition text;
    v_old constant text := $old$WHERE field NOT IN ('phase', 'reason', 'next_action')
           )
           OR NOT (
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
               v_failure_context ? 'phase'
               AND (
                   jsonb_typeof(v_failure_context -> 'phase') IS DISTINCT FROM 'string'
                   OR v_failure_context ->> 'phase' NOT IN (
                       'admission', 'preparation', 'provider-call', 'observation', 'commit'
                   )
               )
           )$old$;
    v_new constant text := $new$WHERE field NOT IN (
               'phase', 'reason', 'next_action', 'cmdline_mismatch'
           )
           )
           OR NOT (
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
               v_failure_context ? 'phase'
               AND (
                   jsonb_typeof(v_failure_context -> 'phase') IS DISTINCT FROM 'string'
                   OR v_failure_context ->> 'phase' NOT IN (
                       'admission', 'preparation', 'provider-call', 'observation', 'commit'
                   )
               )
           )
           OR (
               v_failure_context ? 'cmdline_mismatch'
               AND (
                   p_result ->> 'error_category' IS DISTINCT FROM 'readiness_failure'
                   OR p_result -> 'terminal' IS DISTINCT FROM 'true'::jsonb
                   OR
                   jsonb_typeof(v_failure_context -> 'cmdline_mismatch')
                       IS DISTINCT FROM 'object'
                   OR EXISTS (
                       SELECT 1 FROM jsonb_object_keys(
                           CASE
                               WHEN jsonb_typeof(v_failure_context -> 'cmdline_mismatch') = 'object'
                               THEN v_failure_context -> 'cmdline_mismatch'
                               ELSE '{}'::jsonb
                           END
                       ) AS field
                       WHERE field <> ALL (ARRAY[
                           'schema', 'expected_cmdline', 'observed_cmdline',
                           'first_differing_byte'
                       ])
                   )
                   OR (v_failure_context -> 'cmdline_mismatch') ->> 'schema'
                       IS DISTINCT FROM 'external-boot-cmdline-mismatch-v1'
                   OR jsonb_typeof((v_failure_context -> 'cmdline_mismatch') -> 'expected_cmdline')
                       IS DISTINCT FROM 'string'
                   OR octet_length(
                       (v_failure_context -> 'cmdline_mismatch') ->> 'expected_cmdline'
                   ) > 8192
                   OR jsonb_typeof((v_failure_context -> 'cmdline_mismatch') -> 'observed_cmdline')
                       IS DISTINCT FROM 'string'
                   OR octet_length(
                       (v_failure_context -> 'cmdline_mismatch') ->> 'observed_cmdline'
                   ) > 8192
                   OR jsonb_typeof(
                       (v_failure_context -> 'cmdline_mismatch') -> 'first_differing_byte'
                   ) IS DISTINCT FROM 'number'
                   OR ((v_failure_context -> 'cmdline_mismatch') ->> 'first_differing_byte')
                       !~ '^(0|[1-9][0-9]{0,3})$'
                   OR CASE
                       WHEN ((v_failure_context -> 'cmdline_mismatch')
                           ->> 'first_differing_byte') ~ '^(0|[1-9][0-9]{0,3})$'
                       THEN ((v_failure_context -> 'cmdline_mismatch')
                           ->> 'first_differing_byte')::integer > 2048
                       ELSE TRUE
                   END
               )
           )$new$;
BEGIN
    v_definition := pg_get_functiondef(v_function);
    IF position(v_old in v_definition) = 0 THEN
        RAISE EXCEPTION
            'external-boot command-line diagnostic migration has an unexpected source shape';
    END IF;
    v_definition := replace(v_definition, v_old, v_new);
    EXECUTE v_definition;
END
$$;
