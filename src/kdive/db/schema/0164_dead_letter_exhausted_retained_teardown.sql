-- ADR-0620 amendment (#2917): a `retained_quarantine` teardown receipt on the job's final attempt
-- dead-letters the job instead of requeueing it.  Migration 0147 requeued every retained attempt,
-- so a final one left the job `queued` at `attempt >= max_attempts`, which no worker claims (0150)
-- and no public teardown recycles.  The final attempt now ends the job `failed` and retires the
-- teardown authority, as the `fail` path does at exhaustion (0122): a retired authority keeps the
-- activation's dispatch route for the public teardown's `failed` recycle and can never commit
-- again.  A non-final attempt still requeues (with no category, 0163) and supersedes.  The failed
-- row clears the same lease columns the 0149 retained requeue does and sets its own category.
DO $$
DECLARE
    v_definition text;
    v_old constant text := $old$RETURN 'retained';$old$;
    v_new constant text := $new$IF v_job.attempt >= v_job.max_attempts THEN
            UPDATE public.jobs SET state = 'failed', error_category = 'conflict',
                heartbeat_at = NULL, failure_context = '{}'::jsonb
            WHERE id = p_job_id;
            UPDATE public.external_boot_authorities
            SET state = 'retired', retired_at = clock_timestamp(), superseded_at = NULL
            WHERE id = p_authority_id;
        END IF;
        RETURN 'retained';$new$;
BEGIN
    SELECT pg_get_functiondef(
        'public.finalize_external_boot_authority_teardown('
        'bytea,uuid,integer,uuid,bigint,bigint,text,bytea)'::regprocedure
    ) INTO v_definition;
    IF (length(v_definition) - length(replace(v_definition, v_old, ''))) / length(v_old) <> 1
       OR position('v_job.attempt >= v_job.max_attempts' IN v_definition) <> 0 THEN
        RAISE EXCEPTION 'external boot System teardown retained branch shape changed';
    END IF;
    EXECUTE replace(v_definition, v_old, v_new);
END
$$;

-- Move jobs a pre-0163 retained receipt already stranded, and the superseded root authority of that
-- receipt, to the same states.  No worker can claim such a job, so no attempt races this repair.
WITH stranded AS (
    UPDATE public.jobs SET state = 'failed', error_category = 'conflict',
        heartbeat_at = NULL, failure_context = '{}'::jsonb
    WHERE kind = 'teardown' AND state = 'queued' AND attempt >= max_attempts
      AND jsonb_typeof(payload -> 'external_boot_authority_v1') = 'object'
    RETURNING id, attempt
)
UPDATE public.external_boot_authorities AS authority
SET state = 'retired', retired_at = clock_timestamp(), superseded_at = NULL
FROM stranded
JOIN public.external_boot_teardown_receipts AS receipt
  ON receipt.job_id = stranded.id AND receipt.job_attempt = stranded.attempt
 AND receipt.disposition = 'retained_quarantine'
WHERE authority.id = receipt.root_authority_id
  AND authority.state = 'superseded' AND authority.acknowledged_at IS NOT NULL;
