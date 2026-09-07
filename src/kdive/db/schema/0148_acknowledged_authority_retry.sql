-- Let a new worker replace an acknowledged authority that never began its provider mutation.

DO $$
DECLARE
    v_definition text;
    v_old text :=
        E'        ELSIF v_head.phase = ''terminal'' AND v_head.pending_takeover IS NULL THEN NULL;';
    v_new text :=
        E'        ELSIF v_head.phase IN (''takeover-acknowledged'', ''terminal'')\n' ||
        E'              AND v_head.pending_takeover IS NULL THEN NULL;';
BEGIN
    SELECT pg_get_functiondef(
        'public.advance_external_boot_authority_journal_head(text,uuid,bigint,bigint,text,jsonb)'::regprocedure
    ) INTO v_definition;
    IF position(v_old IN v_definition) = 0 OR position(v_new IN v_definition) <> 0 THEN
        RAISE EXCEPTION 'external boot journal takeover transition shape changed';
    END IF;
    v_definition := replace(v_definition, v_old, v_new);
    IF position(v_old IN v_definition) <> 0 OR position(v_new IN v_definition) = 0 THEN
        RAISE EXCEPTION 'acknowledged authority retry transition was not installed';
    END IF;
    EXECUTE v_definition;
END
$$;
