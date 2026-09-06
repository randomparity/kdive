-- 0137_jobs_payload_run_id_index.sql — bound run-scoped job lookups under a System lock.

CREATE INDEX jobs_payload_run_id_idx ON public.jobs ((payload->>'run_id'));
