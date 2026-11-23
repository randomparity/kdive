-- A device ledger write locks only its credential-derived running job (ADR-0831).
-- The caller holds the System session lock and commits this row lock with its ledger write.
CREATE FUNCTION public.lock_device_job_attempt(
    p_job_id uuid,
    p_credential_hash bytea,
    p_attempt integer,
    p_worker_id text,
    p_created_at timestamptz,
    p_kind text,
    p_payload jsonb
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_incarnation text;
BEGIN
    SELECT w.incarnation INTO v_incarnation
    FROM public.authenticate_worker_incarnation(p_credential_hash) AS w;
    IF v_incarnation IS NULL OR v_incarnation IS DISTINCT FROM p_worker_id THEN
        RETURN false;
    END IF;
    PERFORM 1 FROM public.jobs AS j
    WHERE j.id = p_job_id
      AND j.state = 'running'
      AND j.kind IN ('plug_device', 'unplug_device', 'force_unplug_device')
      AND j.kind = p_kind
      AND j.payload = p_payload
      AND j.worker_id = v_incarnation
      AND j.attempt = p_attempt
      AND j.created_at = p_created_at
    FOR UPDATE;
    RETURN FOUND;
END
$$;

REVOKE ALL ON FUNCTION public.lock_device_job_attempt(
    uuid, bytea, integer, text, timestamptz, text, jsonb
) FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness;
GRANT EXECUTE ON FUNCTION public.lock_device_job_attempt(
    uuid, bytea, integer, text, timestamptz, text, jsonb
) TO kdive_worker;
