-- ADR-0714 amendment (#3396): a release attempt that fails after its derived recovery began
-- commits its failure, and the next attempt of the same job adopts the in-flight recovery.
--
-- 0143 moves a release's activation to `recovering` before the module restore and the provider
-- RECOVER, but the release `fail` admission (0122) names no `recovering` state, so every failure
-- after that point was refused: the job stayed `running` until its lease lapsed, on every
-- attempt.  The failure commit retires the failing authority, as it does for every purpose, so
-- allocation and the begin re-entry join also accept a retired prior authority of the same job.
-- Each function is rewritten in place (as 0128, 0143 and 0170 do) so earlier patches survive.
DO $$
DECLARE
    v_commit constant regprocedure :=
        'public.commit_external_boot_authority_result(bytea,uuid,integer,uuid,bigint,uuid,uuid,uuid,text,text,text,text,text,text,bigint,text,text,jsonb)'::regprocedure;
    v_allocate constant regprocedure :=
        'public.allocate_external_boot_authority(bytea,uuid,integer,uuid,uuid,uuid,text,text,text,text,text)'::regprocedure;
    v_begin constant regprocedure :=
        'public.begin_external_boot_derived_release_recovery(bytea,uuid,integer,uuid,bigint,uuid,timestamptz)'::regprocedure;
    -- The release arm of the `fail` admission.  A recovering activation is admitted only when its
    -- current recovery attempt is the failing authority's own.
    v_old_fail constant text := $old$           OR (p_purpose = 'release' AND (
               v_run.state IS DISTINCT FROM 'succeeded'
               OR v_activation.state NOT IN (
                   'active', 'recovered', 'abandoned',
                   'recovery_conflict', 'recovery_failed'
               )
           ))$old$;
    v_new_fail constant text := $new$           OR (p_purpose = 'release' AND (
               v_run.state IS DISTINCT FROM 'succeeded'
               OR v_activation.state NOT IN (
                   'active', 'recovered', 'abandoned',
                   'recovery_conflict', 'recovery_failed', 'recovering'
               )
               OR (v_activation.state = 'recovering'
                   AND v_attempt.authority_generation IS DISTINCT FROM p_generation)
           ))$new$;
    v_old_allocate constant text := $old$prior.state = 'current'$old$;
    v_new_allocate constant text := $new$prior.state IN ('current', 'retired')$new$;
    v_old_begin constant text := $old$prior.state = 'superseded'$old$;
    v_new_begin constant text := $new$prior.state IN ('superseded', 'retired')$new$;
    v_definition text;
BEGIN
    v_definition := pg_get_functiondef(v_commit);
    IF (length(v_definition) - length(replace(v_definition, v_old_fail, ''))) / length(v_old_fail)
       <> 1 THEN
        RAISE EXCEPTION 'external boot release fail admission shape changed';
    END IF;
    EXECUTE replace(v_definition, v_old_fail, v_new_fail);

    v_definition := pg_get_functiondef(v_allocate);
    IF (length(v_definition) - length(replace(v_definition, v_old_allocate, '')))
       / length(v_old_allocate) <> 1 THEN
        RAISE EXCEPTION 'external boot release allocation adoption shape changed';
    END IF;
    EXECUTE replace(v_definition, v_old_allocate, v_new_allocate);

    v_definition := pg_get_functiondef(v_begin);
    IF (length(v_definition) - length(replace(v_definition, v_old_begin, '')))
       / length(v_old_begin) <> 1 THEN
        RAISE EXCEPTION 'external boot derived release begin adoption shape changed';
    END IF;
    EXECUTE replace(v_definition, v_old_begin, v_new_begin);
END
$$;
