-- 0171_authority_system_discharge.sql — #3143, ADR-0754 (2026-10-12).
-- Forward-only (ADR-0015). An exhausted authority System provision job whose worker died before
-- a terminal receipt stays `running` with no claim or receipt repair. The operator's
-- `systems.teardown` supersedes its attempt; its consumed preactivation-absence receipt is the
-- only evidence that admits allocation release and cancels the stranded predecessor job.

-- The consumed absence receipt is still the current attempt and the durable ownership head.
CREATE FUNCTION public.authority_system_absence_receipt_holds(p_system_id uuid)
RETURNS boolean LANGUAGE sql STABLE SET search_path = '' AS $$
    SELECT EXISTS (
        SELECT 1
        FROM public.authority_system_ownership AS owner
        JOIN public.authority_system_attempts AS receipt
          ON receipt.id = owner.current_attempt_id AND receipt.system_id = owner.system_id
        JOIN public.systems AS system ON system.id = owner.system_id
        WHERE owner.system_id = p_system_id
          AND owner.state = 'torn-down' AND owner.first_activation_id IS NULL
          AND owner.journal_phase = 'terminal'
          AND system.state = 'torn_down'
          AND receipt.operation = 'preactivation-teardown'
          AND receipt.state = 'terminal'
          AND receipt.receipt_disposition = 'preactivation-absent'
          AND receipt.consumed_at IS NOT NULL
          AND receipt.terminal_head_sequence = owner.journal_sequence
          AND receipt.terminal_head_digest = owner.journal_digest
          AND NOT EXISTS (
              SELECT 1 FROM public.authority_system_attempts AS pending
              WHERE pending.system_id = owner.system_id
                AND pending.state IN ('allocating', 'current')
          )
    )
$$;

CREATE FUNCTION public.authority_system_preactivation_absence_proven(p_system_id uuid)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' STABLE AS $$
BEGIN
    IF NOT (pg_has_role(session_user, 'kdive_server', 'member')
            OR pg_has_role(session_user, 'kdive_reconciler', 'member')) THEN
        RAISE EXCEPTION 'control authority is required' USING ERRCODE = '42501';
    END IF;
    RETURN public.authority_system_absence_receipt_holds(p_system_id);
END
$$;

-- An expired, exhausted provision job bound to the exact ownership, enqueued (or recycled) no
-- later than the receipt, whose own attempts were all superseded by the teardown takeover.
CREATE FUNCTION public.authority_system_provision_dischargeable(p_job public.jobs)
RETURNS boolean LANGUAGE sql STABLE SET search_path = '' AS $$
    SELECT p_job.kind = 'provision' AND p_job.state = 'running'
       AND p_job.attempt >= p_job.max_attempts
       AND p_job.lease_expires_at <= clock_timestamp()
       AND jsonb_typeof(p_job.payload -> 'authority_system_v1') = 'object'
       AND p_job.payload #>> '{authority_system_v1,operation}' = 'provision'
       AND EXISTS (
           SELECT 1
           FROM public.authority_system_ownership AS owner
           JOIN public.authority_system_attempts AS receipt
             ON receipt.id = owner.current_attempt_id
           WHERE owner.system_id::text = p_job.payload #>> '{authority_system_v1,system_id}'
             AND owner.allocation_id::text = p_job.payload #>> '{authority_system_v1,allocation_id}'
             AND owner.resource_id::text = p_job.payload #>> '{authority_system_v1,resource_id}'
             AND owner.provider_kind = p_job.payload #>> '{authority_system_v1,provider_kind}'
             AND owner.resource_name = p_job.payload #>> '{authority_system_v1,resource_name}'
             AND owner.authority_instance
                 = p_job.payload #>> '{authority_system_v1,authority_instance}'
             AND owner.profile_identity
                 = p_job.payload #>> '{authority_system_v1,profile_identity}'
             AND owner.root_identity = p_job.payload #>> '{authority_system_v1,root_identity}'
             AND p_job.created_at <= receipt.receipt_at
             AND public.authority_system_absence_receipt_holds(owner.system_id)
             AND NOT EXISTS (
                 SELECT 1 FROM public.authority_system_attempts AS own
                 WHERE own.job_id = p_job.id
                   AND (own.state <> 'superseded' OR own.system_id <> owner.system_id)
             )
       )
$$;

CREATE FUNCTION public.discharge_exhausted_authority_system_provisions(p_limit integer)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_candidate record;
    v_job public.jobs%ROWTYPE;
    v_receipt_digest text;
    v_count integer := 0;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_reconciler', 'member') THEN
        RAISE EXCEPTION 'reconciler authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100 THEN
        RAISE EXCEPTION 'repair limit must be between 1 and 100' USING ERRCODE = '22023';
    END IF;
    FOR v_candidate IN
        SELECT job.id, (job.payload #>> '{authority_system_v1,system_id}')::uuid AS system_id
        FROM public.jobs AS job
        WHERE job.kind = 'provision' AND job.state = 'running'
          AND public.authority_system_provision_dischargeable(job)
        ORDER BY job.id
        LIMIT p_limit
    LOOP
        -- Same order as the authority finalizers; a contended candidate waits for a later pass.
        CONTINUE WHEN NOT pg_try_advisory_xact_lock(
            hashtextextended('kdive:system:' || v_candidate.system_id::text, 2125));
        BEGIN
            PERFORM 1 FROM public.authority_system_ownership
            WHERE system_id = v_candidate.system_id FOR UPDATE NOWAIT;
            PERFORM 1 FROM public.authority_system_attempts
            WHERE system_id = v_candidate.system_id FOR UPDATE NOWAIT;
            SELECT * INTO v_job FROM public.jobs WHERE id = v_candidate.id FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available THEN
            CONTINUE;
        END;
        CONTINUE WHEN NOT public.authority_system_provision_dischargeable(v_job);
        SELECT receipt.receipt_digest INTO v_receipt_digest
        FROM public.authority_system_ownership AS owner
        JOIN public.authority_system_attempts AS receipt ON receipt.id = owner.current_attempt_id
        WHERE owner.system_id = v_candidate.system_id;
        UPDATE public.jobs SET state = 'canceled', worker_id = NULL, lease_expires_at = NULL,
            heartbeat_at = NULL, error_category = NULL, failure_context = '{}'::jsonb,
            result_ref = v_receipt_digest
        WHERE id = v_job.id AND state = 'running';
        v_count := v_count + 1;
    END LOOP;
    RETURN v_count;
END
$$;

REVOKE ALL ON FUNCTION
    public.authority_system_absence_receipt_holds(uuid),
    public.authority_system_provision_dischargeable(public.jobs),
    public.authority_system_preactivation_absence_proven(uuid),
    public.discharge_exhausted_authority_system_provisions(integer)
FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
     kdive_provider_authority;
GRANT EXECUTE ON FUNCTION public.authority_system_preactivation_absence_proven(uuid)
TO kdive_server, kdive_reconciler;
GRANT EXECUTE ON FUNCTION public.discharge_exhausted_authority_system_provisions(integer)
TO kdive_reconciler;
