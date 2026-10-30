-- 0184_cleanup_evidence_drop_resolve_conflict.sql — the cleanup-evidence read no longer admits the
-- resolve-conflict pair (ADR-0620 amendment for #3414, #3432).
--
-- A remote resolve-conflict now commits as a recovery that leaves the recovery objects, so it
-- never commits through cleanup evidence: the authority service's cleanup-evidence commits are
-- only (release, cleanup) and (teardown, teardown). Migrations 0145 and 0182 still listed
-- ('resolve-conflict', 'resolve-conflict') in the read's admitted pairs, which no caller can
-- produce. The function is rewritten in place (as 0181 does) so 0182's derived release CLEANUP
-- match and the null-stripped identity checks survive; only that one pair is removed, and a
-- request carrying it is now refused as invalid facts.
DO $$
DECLARE
    v_read constant regprocedure :=
        'public.read_authorized_remote_module_cleanup_evidence(text,uuid,bigint,bigint,text,uuid,uuid,uuid,text,text,text,text,text,text,text,text)'::regprocedure;
    v_old_pairs constant text := $old$            ('teardown', 'teardown'),
            ('resolve-conflict', 'resolve-conflict')
        )$old$;
    v_new_pairs constant text := $new$            ('teardown', 'teardown')
        )$new$;
    v_definition text;
BEGIN
    v_definition := pg_get_functiondef(v_read);
    IF (length(v_definition) - length(replace(v_definition, v_old_pairs, '')))
       / length(v_old_pairs) <> 1 THEN
        RAISE EXCEPTION 'remote module cleanup evidence admitted pairs shape changed';
    END IF;
    EXECUTE replace(v_definition, v_old_pairs, v_new_pairs);
END
$$;
