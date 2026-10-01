-- ADR-0714 (#2901): a provider-conflict that repeats identically on consecutive attempts of one
-- external-boot job ends the job instead of allocating another authority generation.  The
-- requeue clears the job's failure context (0163), so the audit row keeps the marked context; the
-- commit compares this failure with the previous attempt's under the job-row lock it already
-- holds.  Rewrite in place (as 0160 does) so earlier patches to the function survive.
ALTER TABLE public.external_boot_authority_audit ADD COLUMN failure_context jsonb;

CREATE INDEX external_boot_authority_audit_job_attempt_idx
    ON public.external_boot_authority_audit (job_id, job_attempt);

DO $$
DECLARE
    v_function constant regprocedure :=
        'public.commit_external_boot_authority_result(bytea,uuid,integer,uuid,bigint,uuid,uuid,uuid,text,text,text,text,text,text,bigint,text,text,jsonb)'::regprocedure;
    v_definition text := pg_get_functiondef(v_function);
    v_old_fields constant text := $old$'phase', 'reason', 'next_action', 'cmdline_mismatch'
           )$old$;
    v_new_fields constant text := $new$'phase', 'reason', 'next_action', 'cmdline_mismatch',
               'authority_reason'
           )$new$;
    v_old_phase constant text := $old$           OR (
               v_failure_context ? 'phase'$old$;
    v_new_phase constant text := $new$           OR (
               v_failure_context ? 'authority_reason'
               AND (
                   v_failure_context -> 'authority_reason'
                       IS DISTINCT FROM '"provider-conflict"'::jsonb
                   OR p_result ->> 'error_category' IS DISTINCT FROM 'infrastructure_failure'
               )
           )
           OR (
               v_failure_context ? 'phase'$new$;
    v_old_terminal constant text := $old$v_terminal := (p_result ->> 'terminal')::boolean OR v_job.attempt >= v_job.max_attempts;$old$;
    v_new_terminal constant text := $new$v_terminal := (p_result ->> 'terminal')::boolean
            OR v_job.attempt >= v_job.max_attempts
            OR (
                v_failure_context ? 'authority_reason'
                AND EXISTS (
                    SELECT 1 FROM public.external_boot_authority_audit AS prior
                    WHERE prior.job_id = p_job_id
                      AND prior.job_attempt = v_job.attempt - 1
                      AND prior.created_at >= v_job.created_at
                      AND prior.outcome = 'result_requeued'
                      AND prior.failure_context = v_failure_context
                )
            );$new$;
    v_old_audit constant text := $old$journal_sequence, journal_digest, outcome
    ) VALUES ($old$;
    v_new_audit constant text := $new$journal_sequence, journal_digest, outcome, failure_context
    ) VALUES ($new$;
    v_old_values constant text := $old$p_operation_digest, p_journal_sequence, p_journal_digest, v_outcome
    );$old$;
    v_new_values constant text := $new$p_operation_digest, p_journal_sequence, p_journal_digest, v_outcome,
        CASE WHEN v_operation = 'fail' AND v_failure_context ? 'authority_reason'
             THEN v_failure_context END
    );$new$;
    v_old text;
BEGIN
    FOREACH v_old IN ARRAY ARRAY[
        v_old_fields, v_old_phase, v_old_terminal, v_old_audit, v_old_values
    ] LOOP
        IF (length(v_definition) - length(replace(v_definition, v_old, ''))) / length(v_old) <> 1
        THEN
            RAISE EXCEPTION 'external boot commit shape changed near: %', left(v_old, 60);
        END IF;
    END LOOP;
    v_definition := replace(v_definition, v_old_fields, v_new_fields);
    v_definition := replace(v_definition, v_old_phase, v_new_phase);
    v_definition := replace(v_definition, v_old_terminal, v_new_terminal);
    v_definition := replace(v_definition, v_old_audit, v_new_audit);
    v_definition := replace(v_definition, v_old_values, v_new_values);
    EXECUTE v_definition;
END
$$;
