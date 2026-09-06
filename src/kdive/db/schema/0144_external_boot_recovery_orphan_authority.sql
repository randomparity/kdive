-- Authority-only recovery-object quarantine disposition (#2204).

ALTER TABLE public.external_boot_recovery_orphan_requests
    ADD COLUMN readiness_deadline timestamptz;

UPDATE public.external_boot_recovery_orphan_requests
SET readiness_deadline = created_at
WHERE readiness_deadline IS NULL;

ALTER TABLE public.external_boot_recovery_orphan_requests
    ALTER COLUMN readiness_deadline SET NOT NULL;

REVOKE ALL ON public.external_boot_recovery_quarantine
    FROM kdive_worker;
REVOKE ALL ON public.external_boot_recovery_orphan_requests
    FROM kdive_worker;

CREATE FUNCTION public.resolve_external_boot_recovery_orphan_authority(
    p_peer_incarnation text,
    p_request_id uuid,
    p_job_id uuid,
    p_job_attempt integer
) RETURNS TABLE (
    object_id uuid,
    system_id uuid,
    activation_id uuid,
    run_id uuid,
    disposition text,
    authority_instance text,
    object_kind text,
    object_reference text,
    ownership_digest text,
    observed_digest text,
    operation_identity text,
    attempt_id uuid,
    mutation_journal_sequence bigint,
    mutation_journal_digest text,
    reserved_bytes bigint,
    readiness_deadline timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_request public.external_boot_recovery_orphan_requests%ROWTYPE;
    v_payload jsonb;
    v_expected_status text;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_provider_authority', 'member') THEN
        RAISE EXCEPTION 'provider authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_job_attempt < 1 THEN
        RAISE EXCEPTION 'job attempt is invalid' USING ERRCODE = '22023';
    END IF;

    SELECT request.* INTO v_request
    FROM public.external_boot_recovery_orphan_requests AS request
    JOIN public.jobs AS job ON job.id = request.job_id
    JOIN public.worker_incarnations AS worker ON worker.incarnation = job.worker_id
    WHERE request.id = p_request_id
      AND request.job_id = p_job_id
      AND job.kind = 'resolve_recovery_orphan'
      AND job.state = 'running'
      AND job.attempt = p_job_attempt
      AND job.lease_expires_at > clock_timestamp()
      AND worker.incarnation = p_peer_incarnation
      AND worker.state = 'active'
      AND worker.fence_protocol = 4;
    IF NOT FOUND OR v_request.readiness_deadline <= clock_timestamp() THEN
        RAISE EXCEPTION 'recovery orphan authority is superseded' USING ERRCODE = 'P0001';
    END IF;
    SELECT payload INTO v_payload FROM public.jobs WHERE id = p_job_id;
    IF COALESCE(p_request_id::text = v_payload->>'request_id', false) IS FALSE
       OR COALESCE(v_payload->>'schema' = 'resolve-recovery-orphan-v1', false) IS FALSE
       OR COALESCE(v_payload->>'binding_digest' = v_request.binding_digest, false) IS FALSE
       OR COALESCE((v_payload->'recovery_request_v1'->>'readiness_deadline')::timestamptz
                   = v_request.readiness_deadline, false) IS FALSE THEN
        RAISE EXCEPTION 'recovery orphan request payload is invalid' USING ERRCODE = 'P0001';
    END IF;

    v_expected_status := CASE v_request.disposition
        WHEN 'delete' THEN 'deleted'
        ELSE 'adopted'
    END;
    IF (SELECT count(*) FROM public.external_boot_recovery_quarantine AS q
        WHERE q.id = ANY(v_request.object_ids)
          AND q.system_id = v_request.system_id
          AND q.status IN ('quarantined', v_expected_status)) <> cardinality(v_request.object_ids) THEN
        RAISE EXCEPTION 'recovery orphan selection changed' USING ERRCODE = 'P0001';
    END IF;

    RETURN QUERY
    SELECT q.id, q.system_id, q.activation_id, activation.run_id, v_request.disposition,
           q.authority_instance, q.object_kind, q.object_reference, q.ownership_digest,
           q.observed_digest, q.operation_identity, q.attempt_id, q.mutation_journal_sequence,
           q.mutation_journal_digest, q.reserved_bytes, v_request.readiness_deadline
    FROM public.external_boot_recovery_quarantine AS q
    JOIN public.external_boot_activations AS activation ON activation.id = q.activation_id
    WHERE q.id = ANY(v_request.object_ids)
      AND q.system_id = v_request.system_id
      AND q.status IN ('quarantined', v_expected_status)
    ORDER BY q.object_identity;
END
$$;

CREATE FUNCTION public.commit_external_boot_recovery_orphan_disposition(
    p_peer_incarnation text,
    p_request_id uuid,
    p_job_id uuid,
    p_job_attempt integer,
    p_object_id uuid,
    p_ownership_digest text,
    p_observed_digest text
) RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_request public.external_boot_recovery_orphan_requests%ROWTYPE;
    v_expected_status text;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_provider_authority', 'member') THEN
        RAISE EXCEPTION 'provider authority is required' USING ERRCODE = '42501';
    END IF;
    SELECT request.* INTO v_request
    FROM public.external_boot_recovery_orphan_requests AS request
    JOIN public.jobs AS job ON job.id = request.job_id
    JOIN public.worker_incarnations AS worker ON worker.incarnation = job.worker_id
    WHERE request.id = p_request_id
      AND request.job_id = p_job_id
      AND job.kind = 'resolve_recovery_orphan'
      AND job.state = 'running'
      AND job.attempt = p_job_attempt
      AND job.lease_expires_at > clock_timestamp()
      AND worker.incarnation = p_peer_incarnation
      AND worker.state = 'active'
      AND worker.fence_protocol = 4;
    IF NOT FOUND OR v_request.readiness_deadline <= clock_timestamp() THEN
        RAISE EXCEPTION 'recovery orphan authority is superseded' USING ERRCODE = 'P0001';
    END IF;
    v_expected_status := CASE v_request.disposition
        WHEN 'delete' THEN 'deleted'
        ELSE 'adopted'
    END;
    UPDATE public.external_boot_recovery_quarantine AS q
    SET status = v_expected_status,
        observed_digest = p_observed_digest,
        resolved_at = clock_timestamp(),
        disposition_job_id = p_job_id
    WHERE q.id = p_object_id
      AND q.id = ANY(v_request.object_ids)
      AND q.system_id = v_request.system_id
      AND q.ownership_digest = p_ownership_digest
      AND q.status = 'quarantined';
    IF FOUND THEN
        UPDATE public.external_boot_recovery_orphan_requests AS request
        SET completed_at = clock_timestamp()
        WHERE request.id = p_request_id
          AND request.completed_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM public.external_boot_recovery_quarantine AS q
              WHERE q.id = ANY(request.object_ids) AND q.status = 'quarantined'
          );
        RETURN 'applied';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.external_boot_recovery_quarantine AS q
        WHERE q.id = p_object_id
          AND q.id = ANY(v_request.object_ids)
          AND q.system_id = v_request.system_id
          AND q.ownership_digest = p_ownership_digest
          AND q.status = v_expected_status
          AND q.disposition_job_id = p_job_id
    ) THEN
        RETURN 'applied';
    END IF;
    RAISE EXCEPTION 'recovery orphan disposition is superseded' USING ERRCODE = 'P0001';
END
$$;

REVOKE ALL ON FUNCTION public.resolve_external_boot_recovery_orphan_authority(text, uuid, uuid, integer),
    public.commit_external_boot_recovery_orphan_disposition(text, uuid, uuid, integer, uuid, text, text)
FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness;
GRANT EXECUTE ON FUNCTION public.resolve_external_boot_recovery_orphan_authority(text, uuid, uuid, integer),
    public.commit_external_boot_recovery_orphan_disposition(text, uuid, uuid, integer, uuid, text, text)
TO kdive_provider_authority;
