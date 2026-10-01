-- 0168_tearing_down_authority_teardown.sql — #3026, ADR-0620 amendment (2026-10-01).
-- Forward-only (ADR-0015). A pre-#2966 ordinary teardown can leave a System with external-boot
-- history in `tearing_down`, which only the authority teardown may finish. Admit that state in
-- the three purpose-`teardown` System-state lists: the allocator (0147), the success finalizer
-- (0147, 0149) and the failure commit (0160). Each definition carries the list's tail exactly
-- once. Every other fence, including the `active` Allocation check (#2992), is unchanged.
DO $$
DECLARE
    v_old constant text := $old$'paused', 'crashing', 'crashed', 'failed'$old$;
    v_new constant text := $new$'paused', 'crashing', 'crashed', 'failed', 'tearing_down'$new$;
    v_function regprocedure;
    v_definition text;
BEGIN
    FOREACH v_function IN ARRAY ARRAY[
        'public.allocate_external_boot_authority(bytea,uuid,integer,uuid,uuid,uuid,text,text,'
        'text,text,text)',
        'public.finalize_external_boot_authority_teardown(bytea,uuid,integer,uuid,bigint,bigint,'
        'text,bytea)',
        'public.commit_external_boot_authority_result(bytea,uuid,integer,uuid,bigint,uuid,uuid,'
        'uuid,text,text,text,text,text,text,bigint,text,text,jsonb)'
    ]::regprocedure[] LOOP
        v_definition := pg_get_functiondef(v_function);
        IF (length(v_definition) - length(replace(v_definition, v_old, ''))) / length(v_old) <> 1
           OR position('''tearing_down''' IN v_definition) <> 0 THEN
            RAISE EXCEPTION 'external boot teardown System-state list changed in %', v_function;
        END IF;
        EXECUTE replace(v_definition, v_old, v_new);
    END LOOP;
END
$$;
