-- Admit a bounded optional local timing snapshot on authority journal records (ADR-0684).
-- Existing 26-key records retain their original canonical bytes and digest.

DO $$
DECLARE
    v_definition text;
    v_old text := $old$OR (SELECT count(*) FROM jsonb_object_keys(p_record)) <> 26$old$;
    v_new text := $new$OR (SELECT count(*) FROM jsonb_object_keys(p_record)) NOT IN (26, 27)
       OR ((SELECT count(*) FROM jsonb_object_keys(p_record)) = 27
           AND NOT (p_record ? 'local_timing'))
       OR (p_record ? 'local_timing' AND NOT (
           v_phase NOT IN ('watermark-installed', 'takeover-superseded', 'takeover-acknowledged')
           AND
           jsonb_typeof(p_record->'local_timing') = 'object'
           AND p_record->'local_timing' = jsonb_build_object(
               'schema', p_record #> '{local_timing,schema}',
               'accel', p_record #> '{local_timing,accel}',
               'console_window_s', p_record #> '{local_timing,console_window_s}',
               'deadline_budget_s', p_record #> '{local_timing,deadline_budget_s}'
           )
           AND p_record #>> '{local_timing,schema}' = 'local-external-boot-timing-v1'
           AND (p_record #> '{local_timing,accel}' = 'null'::jsonb
               OR p_record #>> '{local_timing,accel}' IN ('kvm', 'tcg'))
           AND CASE
               WHEN jsonb_typeof(p_record #> '{local_timing,console_window_s}') = 'number'
                AND jsonb_typeof(p_record #> '{local_timing,deadline_budget_s}') = 'number'
               THEN (p_record #>> '{local_timing,console_window_s}')::numeric
                        BETWEEN 1 AND 86399999999999
                AND (p_record #>> '{local_timing,deadline_budget_s}')::numeric
                        BETWEEN 1 AND 86399999999999
                AND (p_record #>> '{local_timing,console_window_s}')::numeric =
                    trunc((p_record #>> '{local_timing,console_window_s}')::numeric)
                AND (p_record #>> '{local_timing,deadline_budget_s}')::numeric =
                    trunc((p_record #>> '{local_timing,deadline_budget_s}')::numeric)
                AND (p_record #>> '{local_timing,deadline_budget_s}')::numeric >
                    (p_record #>> '{local_timing,console_window_s}')::numeric
               ELSE false
           END
       ))$new$;
BEGIN
    SELECT pg_get_functiondef(
        'public.advance_external_boot_authority_journal_head(text,uuid,bigint,bigint,text,jsonb)'::regprocedure
    ) INTO v_definition;
    IF position(v_old IN v_definition) = 0 OR position(v_new IN v_definition) <> 0 THEN
        RAISE EXCEPTION 'external boot journal record shape changed';
    END IF;
    EXECUTE replace(v_definition, v_old, v_new);
END
$$;
