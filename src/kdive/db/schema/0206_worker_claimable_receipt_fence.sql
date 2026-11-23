-- Keep the authority receipt handoff out of claimable-depth sampling (ADR-0846).
DO $$
DECLARE
    v_function constant regprocedure := 'public.count_claimable_worker_jobs(text[])'::regprocedure;
    v_definition text := pg_get_functiondef(v_function);
    v_old constant text := 'AND j.dispatch_lane = ANY(p_accepted_lanes)';
    v_new constant text := E'AND j.dispatch_lane = ANY(p_accepted_lanes)\n' ||
        E'          AND NOT EXISTS (\n' ||
        E'              SELECT 1 FROM public.authority_system_attempts AS authority_attempt\n' ||
        E'              WHERE authority_attempt.job_id=j.id\n' ||
        E'                AND authority_attempt.state=''terminal''\n' ||
        E'                AND authority_attempt.consumed_at IS NULL\n' ||
        E'          )';
BEGIN
    IF (length(v_definition)-length(replace(v_definition,v_old,'')))/length(v_old) <> 1
       OR strpos(v_definition,'authority_attempt.consumed_at IS NULL') <> 0 THEN
        RAISE EXCEPTION 'worker claimable-depth receipt fence source has an unexpected shape';
    END IF;
    EXECUTE replace(v_definition,v_old,v_new);
END
$$;
