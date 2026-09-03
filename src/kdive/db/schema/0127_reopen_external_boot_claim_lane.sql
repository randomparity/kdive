-- Reopen the worker claim path for authority-marked external-boot payloads (ADR-0584, #2201).
--
-- 0122_external_boot_authority.sql closed both the claim path and the generic finalization
-- path against payloads carrying 'external_boot_authority_v1', at a point when no executor
-- existed. The claim-side half is now the single gate that makes every external-boot job
-- unclaimable: a handler registered for an operation can never run, because no worker can
-- take the job and count_claimable_worker_jobs does not see it either.
--
-- This migration reverses exactly that half, on claim_worker_job and
-- count_claimable_worker_jobs. The finalizer-side half on complete_worker_job and
-- fail_worker_job stays installed and is deliberately untouched:
-- commit_external_boot_authority_result finalizes the jobs row itself under SECURITY
-- DEFINER, so reopening the generic finalizers would give an authority-marked job two
-- competing terminalization paths, one of them outside that commit.
--
-- The two injections differ only by an alias -- the claim side is alias-qualified
-- (j.payload), the finalizer side is not (payload). A reversal matching the bare
-- 'external_boot_authority_v1' token would therefore either over-match into the finalizer
-- pair or silently no-op. This removes the full injected claim-side text, newline and
-- trailing indentation included, and then asserts the finalizer pair still carries its own.
--
-- Substring tests use position() rather than LIKE: both the anchor marker (max_attempts)
-- and the token (external_boot_authority_v1) contain '_', which LIKE reads as a
-- single-character wildcard.
DO $$
DECLARE
    v_function regprocedure;
    v_definition text;
    -- The exact text 0122_external_boot_authority.sql:299-303 spliced in ahead of the anchor
    -- marker. The trailing run of spaces is part of what 0122 inserted, so deleting the whole
    -- string restores the body verbatim. The marker's own indentation is never touched, and
    -- no correctness argument here rests on how deep it happens to be.
    v_injected constant text := E'AND NOT (j.payload ? ''external_boot_authority_v1'')\n          ';
    v_marker constant text := 'AND j.attempt < j.max_attempts';
BEGIN
    FOREACH v_function IN ARRAY ARRAY[
        'public.claim_worker_job(text,bytea,interval,text[])'::regprocedure,
        'public.count_claimable_worker_jobs(text[])'::regprocedure
    ] LOOP
        v_definition := pg_get_functiondef(v_function);
        IF position(v_injected || v_marker in v_definition) = 0 THEN
            RAISE EXCEPTION
                'external-boot claim fence reversal has an unexpected source shape for %',
                v_function;
        END IF;
        v_definition := replace(v_definition, v_injected || v_marker, v_marker);
        IF position('external_boot_authority_v1' in v_definition) <> 0 THEN
            RAISE EXCEPTION
                'external-boot claim fence reversal left a residual exclusion in %',
                v_function;
        END IF;
        EXECUTE v_definition;
    END LOOP;
END
$$;

-- Non-regression: the generic finalization fence must survive this migration untouched.
--
-- This block cannot catch an over-match inside the block above. That block loops only over
-- the two claim-side regprocedures, so it never reaches the finalizer pair whatever it
-- matches. What guards against someone extending its array to the finalizer signatures is
-- its own precondition: the alias-qualified injected text does not occur in their bodies, so
-- the loop raises 'unexpected source shape' rather than rewriting them. This block's job is
-- narrower and still worth doing -- refusing to run against a database whose finalization
-- fence something else already removed.
DO $$
DECLARE
    v_function regprocedure;
BEGIN
    FOREACH v_function IN ARRAY ARRAY[
        'public.complete_worker_job(uuid,bytea,integer,text)'::regprocedure,
        'public.fail_worker_job(uuid,bytea,integer,text,jsonb,boolean)'::regprocedure
    ] LOOP
        IF position('external_boot_authority_v1' in pg_get_functiondef(v_function)) = 0 THEN
            RAISE EXCEPTION
                'external-boot generic finalization fence is missing from %', v_function;
        END IF;
    END LOOP;
END
$$;
