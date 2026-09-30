-- ADR-0711 amends ADR-0626 (#2960): one acknowledged-retry grant per exhausted budget.
-- 0150 granted a claim for every new exact acknowledged no-mutation head, so a job whose every
-- attempt the authority refused before provider admission re-claimed at each lease lapse.  The
-- attempt a grant claimed in the current budget now earns no other grant.  Every recycle resets
-- jobs.created_at, so a consumption row older than it belongs to an earlier budget.  Claim, queue
-- depth and consume call has_acknowledged_external_boot_retry_proof by name, so they share the
-- bound.  The 0151 evidence predicate keeps its body under a new name.
ALTER FUNCTION public.has_acknowledged_external_boot_retry_proof(public.jobs)
    RENAME TO has_acknowledged_external_boot_no_mutation_head;

CREATE FUNCTION public.has_acknowledged_external_boot_retry_proof(p_job public.jobs)
RETURNS boolean
LANGUAGE sql
STABLE
RETURNS NULL ON NULL INPUT
SET search_path = ''
AS $$
    SELECT NOT EXISTS (
        SELECT 1
        FROM public.external_boot_acknowledged_retry_consumptions AS consumed
        WHERE consumed.job_id = p_job.id
          AND consumed.claimed_attempt = p_job.attempt
          AND consumed.consumed_at >= p_job.created_at
    ) AND public.has_acknowledged_external_boot_no_mutation_head(p_job)
$$;

REVOKE ALL ON FUNCTION public.has_acknowledged_external_boot_retry_proof(public.jobs)
    FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
         kdive_provider_authority;

-- A boot job past the bound whose head still proves no mutation is failed instead of skipped
-- (ADR-0620's #2889 rule, amended).  The journal-head lock serializes the proof with every head
-- advance; NOWAIT on the authority rows avoids waiting on a commit, which locks them before the
-- job row.  A current authority is retired, as 0164 does, so the activation keeps its dispatch
-- route; an allocating one cannot be retired and is superseded.
CREATE OR REPLACE FUNCTION public.dead_letter_unowned_external_boot_jobs()
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
        IF EXISTS (
            SELECT 1 FROM public.external_boot_authorities AS authority
            WHERE authority.job_id = v_job.id AND authority.state IN ('allocating', 'current')
        ) THEN
            CONTINUE WHEN NOT EXISTS (
                SELECT 1
                FROM public.external_boot_acknowledged_retry_consumptions AS consumed
                WHERE consumed.job_id = v_job.id
                  AND consumed.claimed_attempt = v_job.attempt
                  AND consumed.consumed_at >= v_job.created_at
            );
            PERFORM pg_advisory_xact_lock(hashtextextended(
                'kdive:system:' || (v_job.payload #>> '{external_boot_authority_v1,system_id}'),
                2126
            ));
            CONTINUE WHEN NOT public.has_acknowledged_external_boot_no_mutation_head(v_job);
            BEGIN
                PERFORM 1 FROM public.external_boot_authorities AS authority
                WHERE authority.job_id = v_job.id
                  AND authority.state IN ('allocating', 'current')
                FOR UPDATE NOWAIT;
            EXCEPTION WHEN lock_not_available THEN
                CONTINUE;
            END;
            UPDATE public.external_boot_authorities
            SET state = CASE WHEN state = 'current' THEN 'retired' ELSE 'superseded' END,
                retired_at = CASE WHEN state = 'current' THEN clock_timestamp() END,
                superseded_at = CASE WHEN state = 'allocating' THEN clock_timestamp() END
            WHERE job_id = v_job.id AND state IN ('allocating', 'current');
        END IF;
        UPDATE public.jobs SET state = 'failed', error_category = 'lease_expired'
        WHERE id = v_job.id;
        UPDATE public.runs SET state = 'failed', failure_category = 'lease_expired'
        WHERE id = (v_job.payload #>> '{external_boot_authority_v1,run_id}')::uuid
          AND state IN ('created', 'running');
        RETURN NEXT v_job.id;
    END LOOP;
END
$$;
