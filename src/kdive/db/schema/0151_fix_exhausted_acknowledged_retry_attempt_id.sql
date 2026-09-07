-- Correct the exhausted-acknowledged retry proof to bind the journal attempt to the
-- acknowledged authority's own attempt token, not the worker job id.
--
-- The authority service stores the peer-supplied mutation attempt UUID in
-- journal head_record.attempt_id (provider/external_boot_authority/service.py), while
-- the worker job row key is the durable job id. Comparing attempt_id to p_job.id can
-- never match a genuine acknowledged takeover head, so the recovery gate installed by
-- migration 0150 is unreachable for the real high-water fixture. ADR-0626's exact
-- no-mutation evidence remains intact; this only aligns the binding used to prove that
-- the acknowledged head belongs to the exhausted job.

CREATE OR REPLACE FUNCTION public.has_acknowledged_external_boot_retry_proof(p_job public.jobs)
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
          AND head.head_record->>'attempt_id' = predecessor.id::text
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
