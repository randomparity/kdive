-- 0198_settle_ends_exhausted_boot_job.sql — #3596, ADR-0816 amendment (2026-11-14).
-- Forward-only (ADR-0015). Settlement fails the stranded runs.boot's exhausted, lapsed, ungranted
-- boot job and its Run with lease_expired (the 0165 values), audited, instead of refusing job_live.
-- Exact, asserted replacements of the stored body (the 0150 pattern) keep signature, owner, grants.
DO $migration$
DECLARE
    v_function constant regprocedure :=
        'public.settle_stranded_external_boot_activation(uuid,text,text,text,text)'::regprocedure;
    v_definition text := pg_get_functiondef(v_function);
    v_old text[] := ARRAY[
        $old$    v_reason text := regexp_replace(p_reason, '^\s+|\s+$', '', 'g');$old$,
        $old$    IF EXISTS (SELECT 1 FROM public.jobs AS job
               WHERE job.state IN ('queued', 'running')
                 AND job.payload #>> '{external_boot_authority_v1,system_id}'
                     = v_system.id::text) THEN
        RETURN 'job_live';
    END IF;$old$,
        $old$    UPDATE public.external_boot_authorities
    SET state = 'superseded', superseded_at = clock_timestamp()
    WHERE activation_id = p_activation_id AND state IN ('allocating', 'current');$old$
    ];
    v_new text[] := ARRAY[
        $new$    v_reason text := regexp_replace(p_reason, '^\s+|\s+$', '', 'g');
    v_exhausted uuid[];$new$,
        $new$    BEGIN
        SELECT coalesce(array_agg(exhausted.id ORDER BY exhausted.id), '{}') INTO v_exhausted
        FROM (
            SELECT job.id FROM public.jobs AS job
            WHERE job.kind = 'boot' AND job.state = 'running'
              AND job.attempt >= job.max_attempts
              AND job.lease_expires_at < clock_timestamp()
              AND job.payload #>> '{external_boot_authority_v1,system_id}' = v_system.id::text
              AND job.payload #>> '{external_boot_authority_v1,activation_id}'
                  = p_activation_id::text
              AND NOT public.has_acknowledged_external_boot_retry_proof(job)
            FOR UPDATE OF job NOWAIT
        ) AS exhausted;
    EXCEPTION WHEN lock_not_available THEN
        RETURN 'job_live';
    END;
    IF EXISTS (SELECT 1 FROM public.jobs AS job
               WHERE job.state IN ('queued', 'running')
                 AND job.payload #>> '{external_boot_authority_v1,system_id}'
                     = v_system.id::text
                 AND job.id <> ALL (v_exhausted)) THEN
        RETURN 'job_live';
    END IF;$new$,
        $new$    UPDATE public.jobs SET state = 'failed', error_category = 'lease_expired'
    WHERE id = ANY (v_exhausted);
    UPDATE public.runs SET state = 'failed', failure_category = 'lease_expired'
    WHERE id IN (SELECT (job.payload #>> '{external_boot_authority_v1,run_id}')::uuid
                 FROM public.jobs AS job WHERE job.id = ANY (v_exhausted))
      AND state IN ('created', 'running');
    INSERT INTO public.platform_audit_log (
        principal, agent_session, platform_role, tool, scope, args_digest, actor
    )
    SELECT p_principal, p_agent_session, 'platform_admin',
           'external_boot.settle_stranded_activation', 'failed_job:' || ended.id::text,
           encode(sha256(convert_to(public.canonical_external_boot_authority_json(
               jsonb_build_object('activation_id', p_activation_id::text,
                                  'job_id', ended.id::text, 'reason', v_reason)
           ), 'UTF8')), 'hex'),
           p_actor
    FROM unnest(v_exhausted) AS ended (id);
    UPDATE public.external_boot_authorities
    SET state = 'superseded', superseded_at = clock_timestamp()
    WHERE activation_id = p_activation_id AND state IN ('allocating', 'current');$new$
    ];
BEGIN
    FOR i IN 1 .. array_length(v_old, 1) LOOP
        IF (length(v_definition) - length(replace(v_definition, v_old[i], '')))
           <> length(v_old[i]) THEN
            RAISE EXCEPTION 'settlement function has an unexpected shape at replacement %', i;
        END IF;
        v_definition := replace(v_definition, v_old[i], v_new[i]);
    END LOOP;
    EXECUTE v_definition;
END
$migration$;
