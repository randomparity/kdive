-- ADR-0814 (#3535): a guest free-space refusal keeps its sizes on the job row.  `failure_context`
-- gains the closed `install_refusal` object, valid only on an `install_failure` result.  The commit
-- function is rewritten in place (as 0170, 0181 and 0185 do) so earlier patches to it survive.
CREATE FUNCTION public.external_boot_install_refusal_is_valid(p_refusal jsonb)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE SET search_path = '' AS $$
DECLARE
    v_field text;
    v_value numeric;
BEGIN
    IF jsonb_typeof(p_refusal) IS DISTINCT FROM 'object'
       OR (SELECT count(*) FROM jsonb_object_keys(p_refusal)) <> 3 THEN
        RETURN false;
    END IF;
    FOREACH v_field IN ARRAY ARRAY['vmlinux_bytes', 'free_bytes', 'headroom_bytes'] LOOP
        IF jsonb_typeof(p_refusal -> v_field) IS DISTINCT FROM 'number' THEN
            RETURN false;
        END IF;
        v_value := (p_refusal ->> v_field)::numeric;
        IF v_value <> trunc(v_value) OR v_value NOT BETWEEN 0 AND 9223372036854775807 THEN
            RETURN false;
        END IF;
    END LOOP;
    RETURN (p_refusal ->> 'free_bytes')::numeric
        < (p_refusal ->> 'vmlinux_bytes')::numeric + (p_refusal ->> 'headroom_bytes')::numeric;
END
$$;

REVOKE ALL ON FUNCTION public.external_boot_install_refusal_is_valid(jsonb)
    FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
         kdive_provider_authority;

DO $$
DECLARE
    v_function constant regprocedure :=
        'public.commit_external_boot_authority_result(bytea,uuid,integer,uuid,bigint,uuid,uuid,uuid,text,text,text,text,text,text,bigint,text,text,jsonb)'::regprocedure;
    v_definition text := pg_get_functiondef(v_function);
    v_old_fields constant text := $old$'phase', 'reason', 'next_action', 'cmdline_mismatch',
               'authority_reason'
           )$old$;
    v_new_fields constant text := $new$'phase', 'reason', 'next_action', 'cmdline_mismatch',
               'authority_reason', 'install_refusal'
           )$new$;
    v_old_reason constant text := $old$           OR (
               v_failure_context ? 'authority_reason'
               AND ($old$;
    v_new_reason constant text := $new$           OR (
               v_failure_context ? 'install_refusal'
               AND (
                   p_result ->> 'error_category' IS DISTINCT FROM 'install_failure'
                   OR NOT public.external_boot_install_refusal_is_valid(
                       v_failure_context -> 'install_refusal'
                   )
               )
           )
           OR (
               v_failure_context ? 'authority_reason'
               AND ($new$;
    v_old text;
BEGIN
    FOREACH v_old IN ARRAY ARRAY[v_old_fields, v_old_reason] LOOP
        IF (length(v_definition) - length(replace(v_definition, v_old, ''))) / length(v_old) <> 1
        THEN
            RAISE EXCEPTION 'external boot commit shape changed near: %', left(v_old, 60);
        END IF;
    END LOOP;
    v_definition := replace(v_definition, v_old_fields, v_new_fields);
    v_definition := replace(v_definition, v_old_reason, v_new_reason);
    EXECUTE v_definition;
END
$$;
