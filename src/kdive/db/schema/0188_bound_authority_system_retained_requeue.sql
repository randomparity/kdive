-- 0188_bound_authority_system_retained_requeue.sql -- #3530 (forward-only, ADR-0015).
-- A `retained-quarantine` receipt requeued the authority System job and raised its
-- `max_attempts` by one (0149), so a provision or teardown that fails on every attempt retried
-- without a practical bound.  Once the job has recorded RETAINED_REQUEUE_CAP retained receipts in
-- the current budget, the finalizer and the reconciler repair now end it `failed` (as 0164 does
-- for an external-boot teardown).  `systems.teardown` retires a failed provision job's System; a
-- recycle of the budget (enqueue resets `created_at`) starts the count again.  The cap equals the
-- journal's four-attempt teardown reserve (TEARDOWN_RESERVE_RECORDS, #3526), so a persistently
-- failing teardown cannot spend more than that reserve.

CREATE FUNCTION public.authority_system_retained_budget_spent(p_job public.jobs)
RETURNS boolean LANGUAGE sql STABLE SET search_path = '' AS $$
    SELECT count(*) >= 4
    FROM public.authority_system_attempts AS attempt
    WHERE attempt.job_id = p_job.id
      AND attempt.receipt_disposition = 'retained-quarantine'
      AND attempt.receipt_at >= p_job.created_at
$$;

REVOKE ALL ON FUNCTION public.authority_system_retained_budget_spent(public.jobs)
    FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
         kdive_provider_authority;

DO $$
DECLARE
    v_definition text;
    v_old constant text := $old$        UPDATE public.jobs SET state='queued',worker_id=NULL,lease_expires_at=NULL,
            heartbeat_at=NULL,error_category=NULL,failure_context='{}'::jsonb,
            max_attempts=CASE WHEN attempt>=max_attempts THEN max_attempts+1 ELSE max_attempts END
        WHERE id=p_job_id AND state='running';
        RETURN QUERY SELECT 'retained'::text,'queued'::text,v_system.state; RETURN;$old$;
    v_new constant text := $new$        UPDATE public.jobs SET
            state=CASE WHEN public.authority_system_retained_budget_spent(v_job)
                       THEN 'failed' ELSE 'queued' END,
            worker_id=NULL,lease_expires_at=NULL,heartbeat_at=NULL,
            error_category=CASE WHEN public.authority_system_retained_budget_spent(v_job)
                                THEN 'conflict' ELSE NULL END,
            failure_context='{}'::jsonb,
            max_attempts=CASE WHEN attempt>=max_attempts
                                   AND NOT public.authority_system_retained_budget_spent(v_job)
                              THEN max_attempts+1 ELSE max_attempts END
        WHERE id=p_job_id AND state='running';
        RETURN QUERY SELECT 'retained'::text,
            CASE WHEN public.authority_system_retained_budget_spent(v_job)
                 THEN 'failed' ELSE 'queued' END,v_system.state; RETURN;$new$;
BEGIN
    SELECT pg_get_functiondef(
        'public.finalize_authority_system_attempt(bytea,uuid,integer,uuid,bigint,bigint,text,bytea)'
        ::regprocedure
    ) INTO v_definition;
    IF (length(v_definition) - length(replace(v_definition, v_old, ''))) / length(v_old) <> 1 THEN
        RAISE EXCEPTION 'authority System finalizer retained requeue shape changed';
    END IF;
    EXECUTE replace(v_definition, v_old, v_new);
END
$$;

DO $$
DECLARE
    v_definition text;
    v_old constant text := $old$            UPDATE public.jobs SET state='queued',worker_id=NULL,lease_expires_at=NULL,
                heartbeat_at=NULL,result_ref=NULL,error_category=NULL,failure_context='{}'::jsonb,
                max_attempts=CASE
                    WHEN attempt>=max_attempts THEN max_attempts+1 ELSE max_attempts
                END
            WHERE id=v_attempt.job_id;$old$;
    v_new constant text := $new$            UPDATE public.jobs SET
                state=CASE WHEN public.authority_system_retained_budget_spent(v_job)
                           THEN 'failed' ELSE 'queued' END,
                worker_id=NULL,lease_expires_at=NULL,heartbeat_at=NULL,result_ref=NULL,
                error_category=CASE WHEN public.authority_system_retained_budget_spent(v_job)
                                    THEN 'conflict' ELSE NULL END,
                failure_context='{}'::jsonb,
                max_attempts=CASE
                    WHEN attempt>=max_attempts
                         AND NOT public.authority_system_retained_budget_spent(v_job)
                    THEN max_attempts+1 ELSE max_attempts
                END
            WHERE id=v_attempt.job_id;$new$;
BEGIN
    SELECT pg_get_functiondef(
        'public.repair_terminal_authority_system_attempts(integer)'::regprocedure
    ) INTO v_definition;
    IF (length(v_definition) - length(replace(v_definition, v_old, ''))) / length(v_old) <> 1 THEN
        RAISE EXCEPTION 'authority System repair retained requeue shape changed';
    END IF;
    EXECUTE replace(v_definition, v_old, v_new);
END
$$;
