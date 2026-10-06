-- #2916: the retained_quarantine requeue in the 0147 teardown finalizer kept
-- error_category = 'conflict' on a queued job.  Every other requeue clears it, and the tool
-- envelope (ADR-0019) forbids a category on a non-failure status.  Rewrite in place (as 0149
-- does) so 0149's ownership patch survives; attempt accounting on this path is #2917's.
DO $$
DECLARE
    v_definition text;
    v_old constant text := 'error_category = ''conflict'' WHERE id = p_job_id;';
    v_new constant text := 'error_category = NULL WHERE id = p_job_id;';
BEGIN
    SELECT pg_get_functiondef(
        'public.finalize_external_boot_authority_teardown('
        'bytea,uuid,integer,uuid,bigint,bigint,text,bytea)'::regprocedure
    ) INTO v_definition;
    IF strpos(v_definition, v_old) = 0 THEN
        RAISE EXCEPTION 'external boot teardown retained requeue shape changed';
    END IF;
    v_definition := replace(v_definition, v_old, v_new);
    IF strpos(v_definition, v_new) = 0 THEN
        RAISE EXCEPTION 'external boot teardown retained requeue was not rewritten';
    END IF;
    EXECUTE v_definition;
END
$$;
