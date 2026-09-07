-- Recover a final charged claim that ended after authority acknowledgement but before mutation.
-- ADR-0626 narrows this exception to exact durable no-mutation evidence.
CREATE TABLE public.external_boot_acknowledged_retry_consumptions (
    job_id uuid NOT NULL REFERENCES public.jobs(id) ON DELETE RESTRICT,
    proof_authority_id uuid NOT NULL
        REFERENCES public.external_boot_authorities(id) ON DELETE RESTRICT,
    proof_generation bigint NOT NULL CHECK (proof_generation > 0),
    proof_sequence bigint NOT NULL CHECK (proof_sequence > 0),
    proof_digest text NOT NULL CHECK (proof_digest ~ '^sha256:[0-9a-f]{64}$'),
    claimed_attempt integer NOT NULL CHECK (claimed_attempt > 0),
    consumed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (
        job_id, proof_authority_id, proof_generation, proof_sequence, proof_digest
    ),
    UNIQUE (job_id, claimed_attempt)
);

CREATE TRIGGER external_boot_acknowledged_retry_consumptions_immutable
    BEFORE UPDATE OR DELETE ON public.external_boot_acknowledged_retry_consumptions
    FOR EACH ROW EXECUTE FUNCTION public.reject_external_boot_authority_immutable_row();

REVOKE ALL ON TABLE public.external_boot_acknowledged_retry_consumptions
    FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
         kdive_provider_authority;

CREATE FUNCTION public.has_acknowledged_external_boot_retry_proof(p_job public.jobs)
RETURNS boolean
LANGUAGE sql
STABLE
RETURNS NULL ON NULL INPUT
SET search_path = ''
AS $$
    SELECT jsonb_typeof(p_job.payload->'external_boot_authority_v1') = 'object'
       AND EXISTS (
        SELECT 1
        FROM public.external_boot_authorities AS authority
        JOIN public.external_boot_authority_counters AS counter
          ON counter.system_id = authority.system_id
         AND counter.last_generation = authority.generation
        JOIN public.external_boot_authority_journal_heads AS head
          ON head.authority_instance = authority.authority_instance
         AND head.system_id = authority.system_id
         AND head.operation_identity = authority.operation_identity
        JOIN public.external_boot_authorities AS predecessor
          ON predecessor.id = head.authority_id
         AND predecessor.system_id = head.system_id
         AND predecessor.generation = head.generation
        LEFT JOIN public.external_boot_authority_acknowledgements AS acknowledgement
          ON acknowledgement.authority_id = authority.id
        LEFT JOIN public.external_boot_authority_acknowledgements AS predecessor_acknowledgement
          ON predecessor_acknowledgement.authority_id = predecessor.id
        WHERE authority.job_id = p_job.id
          AND authority.job_attempt = p_job.attempt
          AND authority.worker_incarnation = p_job.worker_id
          AND authority.activation_id::text =
              p_job.payload #>> '{external_boot_authority_v1,activation_id}'
          AND authority.run_id::text =
              p_job.payload #>> '{external_boot_authority_v1,run_id}'
          AND authority.system_id::text =
              p_job.payload #>> '{external_boot_authority_v1,system_id}'
          AND authority.plan_identity =
              p_job.payload #>> '{external_boot_authority_v1,plan_identity}'
          AND authority.purpose = p_job.payload #>> '{external_boot_authority_v1,purpose}'
          AND authority.provider_kind =
              p_job.payload #>> '{external_boot_authority_v1,provider_kind}'
          AND authority.authority_instance =
              p_job.payload #>> '{external_boot_authority_v1,authority_instance}'
          AND authority.operation = p_job.payload #>> '{external_boot_authority_v1,operation}'
          AND authority.operation_identity =
              p_job.payload #>> '{external_boot_authority_v1,operation_identity}'
          AND predecessor.job_id = authority.job_id
          AND predecessor.job_attempt <= authority.job_attempt
          AND predecessor.activation_id = authority.activation_id
          AND predecessor.run_id = authority.run_id
          AND predecessor.system_id = authority.system_id
          AND predecessor.plan_identity = authority.plan_identity
          AND predecessor.purpose = authority.purpose
          AND predecessor.provider_kind = authority.provider_kind
          AND predecessor.authority_instance = authority.authority_instance
          AND predecessor.operation = authority.operation
          AND predecessor.operation_identity = authority.operation_identity
          AND head.phase = 'takeover-acknowledged'
          AND head.pending_takeover IS NULL
          AND head.suspended_operation IS NULL
          AND head.head_record->>'phase' = 'takeover-acknowledged'
          AND head.head_record->>'authority_id' = predecessor.id::text
          AND head.head_record->>'generation' = predecessor.generation::text
          AND head.head_record->>'system_id' = predecessor.system_id::text
          AND head.head_record->>'activation_id' = predecessor.activation_id::text
          AND head.head_record->>'run_id' = predecessor.run_id::text
          AND head.head_record->>'plan_identity' = predecessor.plan_identity
          AND head.head_record->>'purpose' = predecessor.purpose
          AND head.head_record->>'provider_kind' = predecessor.provider_kind
          AND head.head_record->>'authority_instance' = predecessor.authority_instance
          AND head.head_record->>'operation' = predecessor.operation
          AND head.head_record->>'operation_identity' = predecessor.operation_identity
          AND head.head_record->>'operation_digest' = predecessor.operation_digest
          AND head.head_record->>'attempt_id' = p_job.id::text
          AND head.head_record->>'sequence' = head.sequence::text
          AND head.digest = 'sha256:' || encode(sha256(convert_to(
              public.canonical_external_boot_authority_json(head.head_record), 'UTF8'
          )), 'hex')
          AND NOT EXISTS (
              SELECT 1
              FROM public.external_boot_acknowledged_retry_consumptions AS consumed
              WHERE consumed.job_id = p_job.id
                AND consumed.proof_authority_id = predecessor.id
                AND consumed.proof_generation = predecessor.generation
                AND consumed.proof_sequence = head.sequence
                AND consumed.proof_digest = head.digest
          )
          AND (
              (
                  predecessor.id = authority.id
                  AND authority.state = 'allocating'
                  AND acknowledgement.authority_id IS NULL
              )
              OR (
                  predecessor.id = authority.id
                  AND authority.state = 'current'
                  AND acknowledgement.system_id = authority.system_id
                  AND acknowledgement.generation = authority.generation
                  AND acknowledgement.authority_instance = authority.authority_instance
                  AND acknowledgement.operation_identity = authority.operation_identity
                  AND acknowledgement.operation_digest = authority.operation_digest
                  AND acknowledgement.journal_sequence = head.sequence
                  AND acknowledgement.journal_digest = head.digest
              )
              OR (
                  predecessor.generation < authority.generation
                  AND predecessor.state = 'superseded'
                  AND authority.state = 'allocating'
                  AND acknowledgement.authority_id IS NULL
                  AND (
                      predecessor_acknowledgement.authority_id IS NULL
                      OR (
                          predecessor_acknowledgement.system_id = predecessor.system_id
                          AND predecessor_acknowledgement.generation = predecessor.generation
                          AND predecessor_acknowledgement.authority_instance =
                              predecessor.authority_instance
                          AND predecessor_acknowledgement.operation_identity =
                              predecessor.operation_identity
                          AND predecessor_acknowledgement.operation_digest =
                              predecessor.operation_digest
                          AND predecessor_acknowledgement.journal_sequence = head.sequence
                          AND predecessor_acknowledgement.journal_digest = head.digest
                      )
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM public.external_boot_authorities AS intervening
                      LEFT JOIN public.external_boot_authority_acknowledgements AS intervening_ack
                        ON intervening_ack.authority_id = intervening.id
                      WHERE intervening.system_id = authority.system_id
                        AND intervening.generation > predecessor.generation
                        AND intervening.generation <= authority.generation
                        AND (
                            intervening.job_id IS DISTINCT FROM authority.job_id
                            OR intervening.activation_id IS DISTINCT FROM authority.activation_id
                            OR intervening.run_id IS DISTINCT FROM authority.run_id
                            OR intervening.plan_identity IS DISTINCT FROM authority.plan_identity
                            OR intervening.purpose IS DISTINCT FROM authority.purpose
                            OR intervening.provider_kind IS DISTINCT FROM authority.provider_kind
                            OR intervening.authority_instance IS DISTINCT FROM
                                authority.authority_instance
                            OR intervening.operation IS DISTINCT FROM authority.operation
                            OR intervening.operation_identity IS DISTINCT FROM
                                authority.operation_identity
                            OR intervening.job_attempt <= predecessor.job_attempt
                            OR intervening.job_attempt > authority.job_attempt
                            OR intervening_ack.authority_id IS NOT NULL
                            OR (
                                intervening.id = authority.id
                                AND intervening.state <> 'allocating'
                            )
                            OR (
                                intervening.id <> authority.id
                                AND intervening.state <> 'superseded'
                            )
                        )
                  )
              )
          )
    )
$$;

REVOKE ALL ON FUNCTION public.has_acknowledged_external_boot_retry_proof(public.jobs)
    FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
         kdive_provider_authority;

CREATE FUNCTION public.consume_acknowledged_external_boot_retry_proof(p_job public.jobs)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
RETURNS NULL ON NULL INPUT
SET search_path = ''
AS $$
DECLARE
    v_claimed_attempt integer;
BEGIN
    IF p_job.attempt <> p_job.max_attempts OR p_job.max_attempts >= 2147483647
       OR NOT public.has_acknowledged_external_boot_retry_proof(p_job) THEN
        RAISE EXCEPTION 'acknowledged external boot retry proof is not claimable'
            USING ERRCODE = '22023';
    END IF;
    INSERT INTO public.external_boot_acknowledged_retry_consumptions (
        job_id, proof_authority_id, proof_generation, proof_sequence, proof_digest,
        claimed_attempt
    )
    SELECT p_job.id, head.authority_id, head.generation, head.sequence, head.digest,
           p_job.attempt + 1
    FROM public.external_boot_authority_counters AS counter
    JOIN public.external_boot_authorities AS authority
      ON authority.system_id = counter.system_id
     AND authority.generation = counter.last_generation
     AND authority.job_id = p_job.id
     AND authority.job_attempt = p_job.attempt
    JOIN public.external_boot_authority_journal_heads AS head
      ON head.system_id = authority.system_id
     AND head.authority_instance = authority.authority_instance
     AND head.operation_identity = authority.operation_identity
    WHERE public.has_acknowledged_external_boot_retry_proof(p_job)
    ON CONFLICT DO NOTHING
    RETURNING claimed_attempt INTO v_claimed_attempt;
    IF v_claimed_attempt IS NULL THEN
        RAISE EXCEPTION 'acknowledged external boot retry proof was already consumed'
            USING ERRCODE = '40001';
    END IF;
    RETURN p_job.max_attempts + 1;
END
$$;

REVOKE ALL ON FUNCTION public.consume_acknowledged_external_boot_retry_proof(public.jobs)
    FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
         kdive_provider_authority;

DO $$
DECLARE
    v_function regprocedure;
    v_definition text;
    v_attempt_gate constant text := 'AND j.attempt < j.max_attempts';
    v_recovery_gate constant text :=
        E'AND (j.attempt < j.max_attempts\n' ||
        E'               OR (j.attempt = j.max_attempts\n' ||
        E'                   AND j.max_attempts < 2147483647\n' ||
        E'                   AND public.has_acknowledged_external_boot_retry_proof(j)))';
BEGIN
    FOREACH v_function IN ARRAY ARRAY[
        'public.claim_worker_job(text,bytea,interval,text[])'::regprocedure,
        'public.count_claimable_worker_jobs(text[])'::regprocedure
    ] LOOP
        v_definition := pg_get_functiondef(v_function);
        IF position(v_attempt_gate IN v_definition) = 0
           OR position('has_acknowledged_external_boot_retry_proof' IN v_definition) <> 0 THEN
            RAISE EXCEPTION 'worker claim attempt gate has an unexpected shape for %', v_function;
        END IF;
        v_definition := replace(v_definition, v_attempt_gate, v_recovery_gate);
        IF position(v_attempt_gate IN v_definition) <> 0
           OR position(v_recovery_gate IN v_definition) = 0 THEN
            RAISE EXCEPTION 'exhausted authority recovery gate was not installed for %', v_function;
        END IF;
        EXECUTE v_definition;
    END LOOP;
END
$$;

DO $$
DECLARE
    v_definition text;
    v_old constant text :=
        E'        attempt = attempt + 1,\n' ||
        E'        lease_expires_at = v_lease_deadline,';
    v_new constant text :=
        E'        attempt = attempt + 1,\n' ||
        E'        max_attempts = CASE\n' ||
        E'            WHEN attempt = max_attempts\n' ||
        E'            THEN public.consume_acknowledged_external_boot_retry_proof(jobs)\n' ||
        E'            ELSE max_attempts\n' ||
        E'        END,\n' ||
        E'        lease_expires_at = v_lease_deadline,';
BEGIN
    SELECT pg_get_functiondef(
        'public.claim_worker_job(text,bytea,interval,text[])'::regprocedure
    ) INTO v_definition;
    IF position(v_old IN v_definition) = 0 OR position(v_new IN v_definition) <> 0 THEN
        RAISE EXCEPTION 'worker claim attempt update has an unexpected shape';
    END IF;
    v_definition := replace(v_definition, v_old, v_new);
    IF position(v_old IN v_definition) <> 0 OR position(v_new IN v_definition) = 0 THEN
        RAISE EXCEPTION 'exhausted authority recovery attempt update was not installed';
    END IF;
    EXECUTE v_definition;
END
$$;
