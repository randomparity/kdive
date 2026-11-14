-- 0196_authority_system_receipt_pending.sql — #3627, ADR-0620 (2026-11-13 amendment).
-- Forward-only (ADR-0015). `systems.teardown` restarts an exhausted preactivation teardown whose
-- final lease lapsed (#3617). When that attempt already recorded a `preactivation-absent` receipt,
-- the reconciler teardown repair owns the row; a restart would leave a queued row behind a
-- `torn-down` owner. The server cannot read `authority_system_attempts`, so this definer answers
-- the repair candidate's receipt clauses for one System.

CREATE FUNCTION public.authority_system_preactivation_receipt_pending(
    p_system_id uuid, p_job_id uuid
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_server', 'member') THEN
        RAISE EXCEPTION 'server authority is required' USING ERRCODE = '42501';
    END IF;
    -- Every receipt writer and the teardown repair take this lock first; holding it to commit
    -- keeps the answer true for the caller's recycle statement.
    PERFORM pg_advisory_xact_lock(hashtextextended('kdive:system:' || p_system_id::text, 2125));
    RETURN EXISTS (
        SELECT 1
        FROM public.authority_system_attempts AS attempt
        JOIN public.authority_system_ownership AS ownership
          ON ownership.current_attempt_id = attempt.id
        WHERE ownership.system_id = p_system_id AND attempt.job_id = p_job_id
          AND attempt.operation = 'preactivation-teardown'
          AND attempt.state = 'terminal' AND attempt.consumed_at IS NULL
          AND attempt.receipt_disposition = 'preactivation-absent'
          AND ownership.state = 'teardown-requested'
          AND ownership.journal_sequence = attempt.terminal_head_sequence
          AND ownership.journal_digest = attempt.terminal_head_digest
    );
END
$$;

REVOKE ALL ON FUNCTION public.authority_system_preactivation_receipt_pending(uuid, uuid)
FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
     kdive_provider_authority;
GRANT EXECUTE ON FUNCTION public.authority_system_preactivation_receipt_pending(uuid, uuid)
TO kdive_server;
