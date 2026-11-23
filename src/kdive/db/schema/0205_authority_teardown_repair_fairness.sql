-- Keep retryable receipts eligible while yielding the bounded batch to untouched work (ADR-0838).
ALTER TABLE public.authority_system_attempts
    ADD COLUMN teardown_repair_last_retry_at timestamptz;

DO $$
DECLARE
    v_function constant regprocedure :=
        'public.list_authority_system_teardown_repairs(integer)'::regprocedure;
    v_definition text := pg_get_functiondef(v_function);
    v_old constant text := '    ORDER BY attempt.receipt_at,attempt.id';
    v_new constant text := '    ORDER BY COALESCE('
        'attempt.teardown_repair_last_retry_at,attempt.receipt_at),'
        'attempt.receipt_at,attempt.id';
BEGIN
    IF (length(v_definition) - length(replace(v_definition, v_old, ''))) / length(v_old) <> 1
    THEN
        RAISE EXCEPTION 'authority teardown repair ordering shape changed';
    END IF;
    EXECUTE replace(v_definition, v_old, v_new);
END
$$;

CREATE FUNCTION public.mark_authority_system_teardown_repair_retry(
    p_authority_id uuid, p_generation bigint, p_system_id uuid, p_journal_sequence bigint,
    p_journal_digest text, p_receipt_digest text
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_attempt public.authority_system_attempts%ROWTYPE;
    v_owner public.authority_system_ownership%ROWTYPE;
    v_job public.jobs%ROWTYPE;
    v_system public.systems%ROWTYPE;
BEGIN
    IF NOT pg_has_role(session_user,'kdive_reconciler','member') THEN
        RAISE EXCEPTION 'reconciler authority is required' USING ERRCODE='42501';
    END IF;
    IF p_authority_id IS NULL OR p_system_id IS NULL
       OR p_generation IS NULL OR p_generation<1
       OR p_journal_sequence IS NULL OR p_journal_sequence<1
       OR p_journal_digest IS NULL OR p_receipt_digest IS NULL
       OR p_journal_digest!~'^sha256:[0-9a-f]{64}$'
       OR p_receipt_digest!~'^sha256:[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'authority System teardown repair identity is invalid'
        USING ERRCODE='22023';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'kdive:system:' || p_system_id::text,2125));
    SELECT * INTO v_owner FROM public.authority_system_ownership
    WHERE system_id=p_system_id FOR UPDATE;
    SELECT * INTO v_attempt FROM public.authority_system_attempts
    WHERE id=p_authority_id AND generation=p_generation AND system_id=p_system_id FOR UPDATE;
    IF v_attempt.id IS NULL THEN RETURN false; END IF;
    SELECT * INTO v_job FROM public.jobs WHERE id=v_attempt.job_id FOR UPDATE;
    SELECT * INTO v_system FROM public.systems WHERE id=p_system_id FOR UPDATE;
    IF v_owner.current_attempt_id IS DISTINCT FROM v_attempt.id
       OR v_attempt.state IS DISTINCT FROM 'terminal'
       OR v_attempt.consumed_at IS NOT NULL
       OR v_attempt.receipt_disposition IS DISTINCT FROM 'preactivation-absent'
       OR v_attempt.terminal_head_sequence IS DISTINCT FROM p_journal_sequence
       OR v_attempt.terminal_head_digest IS DISTINCT FROM p_journal_digest
       OR v_attempt.receipt_digest IS DISTINCT FROM p_receipt_digest
       OR v_owner.journal_sequence IS DISTINCT FROM p_journal_sequence
       OR v_owner.journal_digest IS DISTINCT FROM p_journal_digest
       OR v_owner.state IS DISTINCT FROM 'teardown-requested'
       OR v_system.id IS NULL OR v_system.state NOT IN ('provisioning','ready','failed')
       OR v_job.id IS NULL
       OR NOT (
           v_job.state<>'running' OR v_job.lease_expires_at<=clock_timestamp()
           OR NOT EXISTS (
               SELECT 1 FROM public.worker_incarnations AS worker
               WHERE worker.incarnation=v_attempt.worker_incarnation AND worker.state='active'
           )
       ) THEN
        RETURN false;
    END IF;
    UPDATE public.authority_system_attempts
    SET teardown_repair_last_retry_at=clock_timestamp()
    WHERE id=v_attempt.id;
    RETURN true;
END
$$;

REVOKE ALL ON FUNCTION public.mark_authority_system_teardown_repair_retry(
    uuid,bigint,uuid,bigint,text,text
) FROM PUBLIC, kdive_server, kdive_worker, kdive_provider_authority;
GRANT EXECUTE ON FUNCTION public.mark_authority_system_teardown_repair_retry(
    uuid,bigint,uuid,bigint,text,text
) TO kdive_reconciler;
