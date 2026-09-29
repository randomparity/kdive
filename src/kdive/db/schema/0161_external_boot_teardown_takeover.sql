-- Take over an interrupted System teardown (#2884, ADR-0620 amendment).
DO $$
DECLARE
    v_definition text;
    v_old constant text := $old$       AND v_head.operation_identity = p_record->>'operation_identity'
       AND (p_record->>'attempt_id' IS DISTINCT FROM v_head.head_record->>'attempt_id'$old$;
    v_new constant text := $new$       AND v_head.operation_identity = p_record->>'operation_identity'
       AND v_phase NOT IN ('watermark-installed', 'takeover-superseded', 'takeover-acknowledged')
       AND (p_record->>'attempt_id' IS DISTINCT FROM v_head.head_record->>'attempt_id'$new$;
BEGIN
    SELECT pg_get_functiondef(
        'public.advance_external_boot_authority_journal_head(text,uuid,bigint,bigint,text,jsonb)'::regprocedure
    ) INTO v_definition;
    IF (length(v_definition) - length(replace(v_definition, v_old, ''))) / length(v_old) <> 1
       OR position(v_new IN v_definition) <> 0 THEN
        RAISE EXCEPTION 'external boot journal same-operation clause shape changed';
    END IF;
    EXECUTE replace(v_definition, v_old, v_new);
END
$$;

-- Only a teardown may allocate over an unresolved teardown (ADR-0620 amendment).
DO $$
DECLARE
    v_definition text;
    v_old constant text := $old$       OR (p_purpose <> 'teardown' AND EXISTS (
           SELECT 1 FROM public.external_boot_authorities AS teardown_authority
           WHERE teardown_authority.system_id = p_system_id
             AND teardown_authority.purpose = 'teardown'
             AND teardown_authority.state = 'current'
       )) THEN$old$;
    v_new constant text := $new$       OR (p_purpose <> 'teardown' AND (EXISTS (
           SELECT 1 FROM public.external_boot_authorities AS teardown_authority
           WHERE teardown_authority.system_id = p_system_id
             AND teardown_authority.purpose = 'teardown'
             AND teardown_authority.state IN ('allocating', 'current')
       ) OR EXISTS (
           SELECT 1 FROM public.external_boot_authority_journal_heads AS teardown_head
           WHERE teardown_head.system_id = p_system_id
             AND (teardown_head.suspended_operation->>'purpose' = 'teardown'
                  OR (teardown_head.phase IN (
                          'admitted', 'mutation-started', 'provider-returned', 'observed'
                      )
                      AND teardown_head.head_record->>'purpose' = 'teardown'))
       ))) THEN$new$;
BEGIN
    SELECT pg_get_functiondef((
        'public.allocate_external_boot_authority('
        || 'bytea,uuid,integer,uuid,uuid,uuid,text,text,text,text,text)'
    )::regprocedure) INTO v_definition;
    IF (length(v_definition) - length(replace(v_definition, v_old, ''))) / length(v_old) <> 1
       OR position(v_new IN v_definition) <> 0 THEN
        RAISE EXCEPTION 'external boot teardown allocation fence shape changed';
    END IF;
    EXECUTE replace(v_definition, v_old, v_new);
END
$$;
