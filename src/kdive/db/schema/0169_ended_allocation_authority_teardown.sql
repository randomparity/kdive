-- 0169_ended_allocation_authority_teardown.sql — #2992, ADR-0584/ADR-0620 amendments (2026-10-01).
-- Forward-only (ADR-0015). Lease expiry ends an Allocation whose System may still have
-- external-boot history, and only the authority teardown may finish that System. Admit purpose
-- `teardown` on an Allocation in any state in the allocator, the acknowledgement and the result
-- commit (0122). Every other purpose keeps the `active` fence. Each definition carries the fence
-- literal exactly once; each validates `p_purpose` as non-null before it and binds it to the
-- marked or stored purpose in the same predicate.
DO $$
DECLARE
    v_old constant text := $old$v_allocation.state <> 'active'$old$;
    v_new constant text := $new$(v_allocation.state <> 'active' AND p_purpose <> 'teardown')$new$;
    v_function regprocedure;
    v_definition text;
BEGIN
    FOREACH v_function IN ARRAY ARRAY[
        'public.allocate_external_boot_authority(bytea,uuid,integer,uuid,uuid,uuid,text,text,'
        'text,text,text)',
        'public.acknowledge_external_boot_authority(uuid,bigint,uuid,uuid,uuid,uuid,text,uuid,'
        'integer,text,text,text,text,text,text,text,bigint,text,text)',
        'public.commit_external_boot_authority_result(bytea,uuid,integer,uuid,bigint,uuid,uuid,'
        'uuid,text,text,text,text,text,text,bigint,text,text,jsonb)'
    ]::regprocedure[] LOOP
        v_definition := pg_get_functiondef(v_function);
        IF (length(v_definition) - length(replace(v_definition, v_old, ''))) / length(v_old) <> 1
           OR position(v_new IN v_definition) <> 0 THEN
            RAISE EXCEPTION 'external boot authority Allocation fence changed in %', v_function;
        END IF;
        EXECUTE replace(v_definition, v_old, v_new);
    END LOOP;
END
$$;
