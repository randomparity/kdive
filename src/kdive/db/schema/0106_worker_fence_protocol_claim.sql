-- Database-enforced current-protocol job claims (ADR-0533, #1803).

CREATE FUNCTION enforce_current_worker_fence_protocol()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
    IF NEW.state = 'running'
       AND (
           OLD.state IS DISTINCT FROM 'running'
           OR NEW.worker_id IS DISTINCT FROM OLD.worker_id
           OR NEW.attempt IS DISTINCT FROM OLD.attempt
       )
       AND (
           NEW.worker_id IS NULL
           OR NOT EXISTS (
               SELECT 1
               FROM public.worker_incarnations AS w
               WHERE w.incarnation = NEW.worker_id
                 AND w.state = 'active'
                 AND w.fence_protocol = 2
           )
       ) THEN
        RAISE EXCEPTION 'current active worker fence protocol is required for job claim'
            USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER jobs_current_worker_fence_protocol
BEFORE UPDATE ON public.jobs
FOR EACH ROW
EXECUTE FUNCTION enforce_current_worker_fence_protocol();

CREATE FUNCTION claim_worker_job(
    p_worker_id text,
    p_credential_hash bytea,
    p_lease interval,
    p_accepted_lanes text[]
) RETURNS SETOF public.jobs
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_incarnation text;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_worker_id IS NULL
       OR octet_length(p_worker_id) NOT BETWEEN 1 AND 512
       OR p_credential_hash IS NULL
       OR octet_length(p_credential_hash) <> 32
       OR p_lease IS NULL
       OR p_accepted_lanes IS NULL THEN
        RAISE EXCEPTION 'worker claim facts are invalid' USING ERRCODE = '22023';
    END IF;

    SELECT w.incarnation INTO v_incarnation
    FROM public.worker_incarnations AS w
    WHERE w.credential_hash = p_credential_hash;
    IF v_incarnation IS NULL OR v_incarnation <> p_worker_id THEN
        RETURN;
    END IF;

    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:worker-incarnation:' || v_incarnation, 1803)
    );
    PERFORM 1
    FROM public.worker_incarnations AS w
    WHERE w.incarnation = v_incarnation
      AND w.credential_hash = p_credential_hash
      AND w.state = 'active'
      AND w.fence_protocol = 2
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN;
    END IF;

    RETURN QUERY
    UPDATE public.jobs
    SET state = 'running',
        worker_id = v_incarnation,
        attempt = attempt + 1,
        lease_expires_at = now() + p_lease,
        heartbeat_at = now()
    WHERE id = (
        SELECT id
        FROM public.jobs
        WHERE (
            state = 'queued'
            OR (state = 'running' AND lease_expires_at < now())
        )
          AND attempt < max_attempts
          AND dispatch_lane = ANY(p_accepted_lanes)
        ORDER BY created_at
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING *;
END
$$;

REVOKE ALL ON FUNCTION enforce_current_worker_fence_protocol() FROM PUBLIC;
REVOKE ALL ON FUNCTION claim_worker_job(text, bytea, interval, text[]) FROM PUBLIC;
REVOKE ALL ON FUNCTION
    enforce_current_worker_fence_protocol(),
    claim_worker_job(text, bytea, interval, text[])
FROM kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness;

GRANT EXECUTE ON FUNCTION claim_worker_job(text, bytea, interval, text[]) TO kdive_worker;
