-- 0116_capture_claimable_queue_depth.sql — least-privilege worker queue-depth telemetry.
--
-- A worker cannot read capture_operations directly. Keep the capture-aware claimability predicate
-- behind one aggregate-only SECURITY DEFINER function so telemetry cannot expose operation rows.
CREATE FUNCTION public.count_claimable_worker_jobs(p_accepted_lanes text[])
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_accepted_lanes IS NULL
       OR cardinality(p_accepted_lanes) = 0
       OR EXISTS (
           SELECT 1
           FROM pg_catalog.unnest(p_accepted_lanes) AS lane(value)
           WHERE lane.value IS NULL OR lane.value = ''
       ) THEN
        RAISE EXCEPTION 'worker claimable-depth lanes are invalid' USING ERRCODE = '22023';
    END IF;

    RETURN (
        SELECT count(*)
        FROM public.jobs AS j
        WHERE (
            j.state = 'queued'
            OR (j.state = 'running' AND j.lease_expires_at < now())
        )
          AND j.attempt < j.max_attempts
          AND j.dispatch_lane = ANY(p_accepted_lanes)
          AND (
              j.kind <> 'capture_traffic'
              OR NOT EXISTS (
                  SELECT 1
                  FROM public.capture_operations AS o
                  WHERE o.job_id = j.id
                    AND (
                        o.state <> 'exited'
                        OR NOT o.process_absent
                        OR o.provider_quiescence = '{}'::jsonb
                        OR o.publication_state NOT IN ('published', 'discarded')
                        OR o.spool_disposed_at IS NULL
                    )
              )
          )
    );
END
$$;

REVOKE ALL ON FUNCTION public.count_claimable_worker_jobs(text[]) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.count_claimable_worker_jobs(text[])
    FROM kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness;
GRANT EXECUTE ON FUNCTION public.count_claimable_worker_jobs(text[]) TO kdive_worker;
