-- 0038_runs_failing_job_id.sql — link a failed Run to the job that carries its
-- human-readable reason (ADR-0141, #486). A build-failed Run previously surfaced only
-- its failure_category; the redacted reason lives on the BUILD job's failure_context,
-- reachable via jobs.get on an id the caller had to know out-of-band. _fail_build sets
-- this column atomically with the running -> failed transition; runs.get fetches the
-- linked job and surfaces its already-worker-redacted failure_message as the envelope
-- detail.
--
-- Additive, forward-only (ADR-0015): NULL for every existing Run and for any failed
-- path that has no job (e.g. a Run failed by the reconciler on a torn-down System), in
-- which case runs.get degrades to today's category-only envelope.
--
-- Plain column, NOT a foreign key: `jobs` rows are never deleted (no retention/purge
-- path exists), so the reference can never dangle, and a FK would force insert-ordering
-- on Run creation for no integrity gain. Not an enum column, so no CHECK / no
-- CHECK_ENUMS registration in test_migrate.py.
ALTER TABLE runs
    ADD COLUMN failing_job_id uuid;
