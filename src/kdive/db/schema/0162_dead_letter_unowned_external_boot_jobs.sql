-- ADR-0620 amendment (#2889): dead-letter an exhausted authority-marked job that no receipt can
-- ever finish.  repair_abandoned_jobs skips marked payloads because their receipt paths own
-- terminalization, so a `boot` job (a marker's only kind besides `teardown`, 0122) that its
-- authority refused (superseded, binding mismatch) stayed `running` forever once its final lease
-- lapsed.  Every receipt path needs an `allocating` or `current` authority row for the job and a
-- `running` job row, and allocation rechecks the job under its own row lock.  So the job row is
-- locked first, and a later statement (a fresh READ COMMITTED snapshot) requires that no such
-- authority row exists; nothing can then commit for the job.  Teardown jobs are left to the
-- public teardown recycle.  The reconciler cannot read authority rows, hence security definer.
CREATE FUNCTION public.dead_letter_unowned_external_boot_jobs()
RETURNS SETOF uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_job public.jobs%ROWTYPE;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_reconciler', 'member') THEN
        RAISE EXCEPTION 'reconciler authority is required' USING ERRCODE = '42501';
    END IF;
    FOR v_job IN
        SELECT j.* FROM public.jobs AS j
        WHERE j.state = 'running' AND j.kind = 'boot'
          AND j.attempt >= j.max_attempts
          AND j.lease_expires_at < clock_timestamp()
          AND jsonb_typeof(j.payload -> 'external_boot_authority_v1') = 'object'
        ORDER BY j.id
        FOR UPDATE OF j SKIP LOCKED
    LOOP
        CONTINUE WHEN EXISTS (
            SELECT 1 FROM public.external_boot_authorities AS authority
            WHERE authority.job_id = v_job.id AND authority.state IN ('allocating', 'current')
        );
        UPDATE public.jobs SET state = 'failed', error_category = 'lease_expired'
        WHERE id = v_job.id;
        UPDATE public.runs SET state = 'failed', failure_category = 'lease_expired'
        WHERE id = (v_job.payload #>> '{external_boot_authority_v1,run_id}')::uuid
          AND state IN ('created', 'running');
        RETURN NEXT v_job.id;
    END LOOP;
END
$$;

REVOKE ALL ON FUNCTION public.dead_letter_unowned_external_boot_jobs()
FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
    kdive_provider_authority;
GRANT EXECUTE ON FUNCTION public.dead_letter_unowned_external_boot_jobs() TO kdive_reconciler;
